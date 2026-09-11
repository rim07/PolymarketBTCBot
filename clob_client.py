"""Thin wrapper around polymarket-client's SecureClient: the only module
allowed to touch the authenticated CLOB API. Switched here (from
py-clob-client-v2) after confirming that client cannot produce the ERC-7739
nested-signature format Polymarket's backend now requires for deposit-wallet
accounts ("maker address not allowed, please use the deposit wallet flow" —
confirmed as an open, unfixed bug: Polymarket/py-clob-client-v2#111).
polymarket-client has a real, working implementation of this flow
(polymarket._internal.actions.orders.typed_data), confirmed by reading its
source directly.

SecureClient.create(private_key=...) with no explicit `wallet` derives your
deposit wallet address and deploys it automatically on first use if it isn't
deployed yet — no separate setup script needed. Field/method names below are
taken verbatim from the installed polymarket-client source; confirm against
your installed version in the smoke test before going live.
"""
import logging
from dataclasses import dataclass

from polymarket.auth import RelayerApiKey
from polymarket.clients.secure import SecureClient

import config
from edge import OrderBookTop

log = logging.getLogger("btcbot.clob")

_client: SecureClient | None = None


def get_client() -> SecureClient:
    global _client
    if _client is not None:
        return _client

    if not config.PRIVATE_KEY:
        raise RuntimeError("PK is not set in .env — cannot initialize the CLOB client.")

    api_key = None
    if config.RELAYER_API_KEY and config.RELAYER_API_KEY_ADDRESS:
        api_key = RelayerApiKey(key=config.RELAYER_API_KEY, address=config.RELAYER_API_KEY_ADDRESS)

    _client = SecureClient.create(private_key=config.PRIVATE_KEY, api_key=api_key)
    return _client


def get_deposit_wallet_address() -> str:
    return get_client().wallet


def get_book_top(up_token_id: str, down_token_id: str) -> OrderBookTop:
    client = get_client()
    up_book, down_book = client.get_order_books(token_ids=[up_token_id, down_token_id])
    if up_book.asset_id != up_token_id:
        up_book, down_book = down_book, up_book

    def best(levels, pick_max: bool) -> tuple[float, float]:
        if not levels:
            return (0.0, 0.0)
        level = max(levels, key=lambda lv: lv.price) if pick_max else min(levels, key=lambda lv: lv.price)
        return float(level.price), float(level.size)

    up_bid, _ = best(up_book.bids, pick_max=True)
    up_ask, up_ask_size = best(up_book.asks, pick_max=False)
    down_bid, _ = best(down_book.bids, pick_max=True)
    down_ask, down_ask_size = best(down_book.asks, pick_max=False)

    return OrderBookTop(
        up_bid=up_bid,
        up_ask=up_ask,
        up_ask_size=up_ask_size,
        down_bid=down_bid,
        down_ask=down_ask,
        down_ask_size=down_ask_size,
    )


def get_min_order_size(token_id: str) -> float:
    client = get_client()
    (book,) = client.get_order_books(token_ids=[token_id])
    return float(book.min_order_size)


def get_collateral_balance_usdc() -> float:
    client = get_client()
    result = client.get_balance_allowance(asset_type="COLLATERAL")
    return result.balance / 1e6  # collateral uses 6 decimals


def get_outcome_token_balance(token_id: str) -> float:
    """Direct balance for one outcome token — used by the risk manager to
    confirm open-position state independent of local JSON state."""
    client = get_client()
    result = client.get_balance_allowance(asset_type="CONDITIONAL", token_id=token_id)
    return result.balance / 1e6


@dataclass
class PlacedOrder:
    order_id: str
    status: str  # "matched" | "live" | "delayed" — see AcceptedOrder.status
    filled_shares: float
    filled_usdc: float
    trade_ids: tuple[str, ...]

    @property
    def filled(self) -> bool:
        """True only when the exchange actually matched this order at placement.

        `resp.ok` is NOT this: an accepted order with status "live" is resting
        unfilled on the book, and "delayed" hasn't been decided yet. Treating
        either as a fill is what created phantom positions — local state showing
        a position that doesn't exist, which blocks every later window and then
        books a fabricated full-stake loss when the market resolves.
        """
        return self.status == "matched" and bool(self.trade_ids) and self.filled_shares > 0.0


_BASE_UNIT_SCALE = 1_000_000  # USDC and outcome tokens are both 6-decimal on Polygon


def _resolve_fill(
    resp, *, intended_shares: float, intended_usdc: float, limit_price: float, side: str
) -> tuple[float, float]:
    """Actual (filled_shares, filled_usdc) from an accepted order response.

    `making_amount`/`taking_amount` are the two legs of the fill — for a BUY,
    (USDC paid, shares received); for a SELL, (shares sold, USDC received) —
    but which field holds which leg isn't documented in the installed client,
    so we don't assume. Instead we use an invariant that holds either way:
    every limit price this desk can submit is <= 0.99, so shares = usdc / price
    is always strictly larger than usdc. The bigger leg is the share count and
    the smaller is the USDC amount, whichever field each arrived in.

    Falls back to the intended amounts (and says so loudly) if the response
    can't be interpreted, so a reporting surprise can never silently overstate
    or understate a real position.
    """
    legs = sorted((float(resp.making_amount), float(resp.taking_amount)))
    filled_usdc, filled_shares = legs[0], legs[1]

    if filled_shares <= 0.0:
        return 0.0, 0.0

    # Scale-invariant, so this check works whether the legs arrived in human
    # units or 6-decimal base units. The bound is the exchange's own price
    # guarantee rather than a generic (0, 1]: a limit order never fills worse
    # than its limit, so a BUY's implied price can't exceed limit_price and a
    # SELL's can't fall below it. That's what catches legs we've decoded the
    # wrong way round — two equal legs imply a price of exactly 1.0, which
    # passes a (0, 1] test but is impossible for any order this desk submits.
    implied_price = filled_usdc / filled_shares
    tolerance = 0.001
    price_ok = (
        implied_price <= limit_price + tolerance if side == "BUY"
        else implied_price >= limit_price - tolerance
    )
    if implied_price <= 0.0 or not price_ok:
        log.warning(
            "Order %s (%s, limit %.3f): uninterpretable fill legs making=%s taking=%s "
            "(implied price %.4f); falling back to intended %.2f shares / $%.2f.",
            resp.order_id, side, limit_price, resp.making_amount, resp.taking_amount,
            implied_price, intended_shares, intended_usdc,
        )
        return intended_shares, intended_usdc

    if filled_shares > intended_shares * 100:
        filled_shares /= _BASE_UNIT_SCALE
        filled_usdc /= _BASE_UNIT_SCALE

    if filled_shares > intended_shares * 1.01:
        log.warning(
            "Order %s reports %.4f shares filled against %.4f intended — using the intended "
            "amount rather than trusting an over-fill.", resp.order_id, filled_shares, intended_shares,
        )
        return intended_shares, intended_usdc

    if filled_shares < intended_shares * 0.999:
        log.warning(
            "Order %s PARTIALLY filled: %.4f of %.4f shares ($%.2f of $%.2f) at ~%.3f (%s).",
            resp.order_id, filled_shares, intended_shares, filled_usdc, intended_usdc, implied_price, side,
        )

    return filled_shares, filled_usdc


def place_limit_buy(token_id: str, limit_price: float, stake_usdc: float) -> PlacedOrder:
    """Places a GTC limit buy. size is in shares; stake_usdc / limit_price gives
    the share count that costs exactly stake_usdc at that price.

    Returns a PlacedOrder whose `.filled` tells you whether anything actually
    traded — always check it. An unfilled order is still live on the book and
    the caller is responsible for cancelling it.
    """
    client = get_client()
    size_shares = round(stake_usdc / limit_price, 2)
    resp = client.place_limit_order(token_id=token_id, price=limit_price, size=size_shares, side="BUY")
    if not resp.ok:
        raise RuntimeError(f"order rejected: {resp.code}: {resp.message}")
    filled_shares, filled_usdc = _resolve_fill(
        resp, intended_shares=size_shares, intended_usdc=stake_usdc, limit_price=limit_price, side="BUY",
    )
    return PlacedOrder(
        order_id=resp.order_id, status=resp.status, filled_shares=filled_shares,
        filled_usdc=filled_usdc, trade_ids=tuple(resp.trade_ids),
    )


def place_limit_sell(token_id: str, limit_price: float, size_shares: float) -> PlacedOrder:
    """Places a GTC limit sell. Check `.filled` before treating the position as
    closed — an unfilled sell means you still hold the shares."""
    client = get_client()
    size_shares = round(size_shares, 2)
    resp = client.place_limit_order(token_id=token_id, price=limit_price, size=size_shares, side="SELL")
    if not resp.ok:
        raise RuntimeError(f"order rejected: {resp.code}: {resp.message}")
    filled_shares, filled_usdc = _resolve_fill(
        resp, intended_shares=size_shares, intended_usdc=size_shares * limit_price,
        limit_price=limit_price, side="SELL",
    )
    return PlacedOrder(
        order_id=resp.order_id, status=resp.status, filled_shares=filled_shares,
        filled_usdc=filled_usdc, trade_ids=tuple(resp.trade_ids),
    )


def cancel_order(order_id: str) -> None:
    """Best-effort cancel of one resting order. Used to clean up a limit order
    that didn't match at placement — leaving it on the book in a market that's
    about to resolve is an uncontrolled position."""
    get_client().cancel_order(order_id=order_id)


def redeem(condition_id: str) -> str:
    """Redeems a resolved position. For a deposit-wallet account this goes
    through the gasless relay, not a self-paid transaction. Blocks until
    the transaction reaches a terminal state; returns the transaction hash."""
    client = get_client()
    handle = client.redeem_positions(condition_id=condition_id)
    result = handle.wait()
    return str(result.transaction_hash)


def cancel_all_orders() -> None:
    get_client().cancel_all()
