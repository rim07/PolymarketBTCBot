"""Live BTC price polling.

Primary feed: Binance/Coinbase spot ticker (fast, public, no auth) — reused
retry/backoff pattern from the existing btc_session_candles.py/ny_open_btc_candle.py
scripts in Downloads.

Secondary/cross-check feed: the Chainlink BTC/USD aggregator on Polygon, which is
what Polymarket's "Bitcoin Up or Down" markets actually settle against (confirmed:
resolution is a Chainlink BTC/USD TWAP over the 5-minute window, NOT raw exchange
spot). If the two feeds diverge meaningfully, signal.py should not be trusted —
see the "resolution price-feed mismatch" risk in the plan.
"""
import json
import time
import urllib.error
import urllib.request

import config

_web3 = None
_chainlink_feed = None

# Minimal ABI: only the one function we need.
_AGGREGATOR_ABI = [
    {
        "inputs": [],
        "name": "latestRoundData",
        "outputs": [
            {"name": "roundId", "type": "uint80"},
            {"name": "answer", "type": "int256"},
            {"name": "startedAt", "type": "uint256"},
            {"name": "updatedAt", "type": "uint256"},
            {"name": "answeredInRound", "type": "uint80"},
        ],
        "stateMutability": "view",
        "type": "function",
    }
]


def http_json(url, retries=3, timeout=10):
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "btcbot/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code in (418, 429) and attempt < retries - 1:
                time.sleep(2 ** attempt)
                continue
            raise
        except urllib.error.URLError:
            if attempt < retries - 1:
                time.sleep(1 + attempt)
                continue
            raise
    return None


def fetch_binance_price(symbol: str = "BTCUSDT") -> float:
    data = http_json(f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}")
    return float(data["price"])


def fetch_coinbase_price(symbol: str = "BTC-USD") -> float:
    data = http_json(f"https://api.exchange.coinbase.com/products/{symbol}/ticker")
    return float(data["price"])


def fetch_spot_price() -> float:
    """Best-effort spot price: Binance first, Coinbase as fallback."""
    try:
        return fetch_binance_price()
    except Exception:
        return fetch_coinbase_price()


def fetch_chainlink_btcusd() -> float:
    """Read the on-chain Chainlink BTC/USD feed used for settlement. Read-only, no gas."""
    global _web3, _chainlink_feed
    if _chainlink_feed is None:
        from web3 import Web3

        _web3 = Web3(Web3.HTTPProvider(config.POLYGON_RPC_URL))
        _chainlink_feed = _web3.eth.contract(
            address=Web3.to_checksum_address(config.CHAINLINK_BTCUSD_FEED_ADDRESS),
            abi=_AGGREGATOR_ABI,
        )
    _, answer, _, updated_at, _ = _chainlink_feed.functions.latestRoundData().call()
    return answer / 1e8, updated_at


class RollingPriceTracker:
    """In-process buffer of (timestamp, price) samples for the current window,
    used to compute short-term momentum/volatility for signal.py without
    depending on exchange candle granularity."""

    def __init__(self):
        self.samples: list[tuple[float, float]] = []

    def reset(self):
        self.samples.clear()

    def add(self, price: float, ts: float | None = None):
        self.samples.append((ts if ts is not None else time.time(), price))

    def open_price(self) -> float | None:
        return self.samples[0][1] if self.samples else None

    def last_price(self) -> float | None:
        return self.samples[-1][1] if self.samples else None

    def momentum_bps(self) -> float:
        """Price change since window open, in basis points."""
        if len(self.samples) < 2:
            return 0.0
        o, c = self.samples[0][1], self.samples[-1][1]
        return (c - o) / o * 10_000

    def realized_vol_bps(self) -> float:
        """Simple realized volatility proxy: mean absolute tick-to-tick move in bps."""
        if len(self.samples) < 3:
            return 0.0
        moves = []
        for (_, p0), (_, p1) in zip(self.samples, self.samples[1:]):
            moves.append(abs(p1 - p0) / p0 * 10_000)
        return sum(moves) / len(moves)
