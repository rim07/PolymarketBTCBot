"""Thin wrapper around py-clob-client-v2: the only module allowed to touch the
authenticated CLOB API. Switched from v1 (py-clob-client) after confirming
v1 can no longer place orders at all ("invalid order version, please use the
latest clob-client") — Polymarket has moved order placement to a new schema
that only v2 speaks. Field names below are taken verbatim from
py-clob-client-v2's clob_types.py — confirm against your installed version
in the smoke test before going live.
"""
from dataclasses import dataclass

from py_clob_client_v2 import ApiCreds, ClobClient, OrderType, Side
from py_clob_client_v2.clob_types import AssetType, BalanceAllowanceParams, BookParams
from py_clob_client_v2.clob_types import OrderArgs as OrderArgsV2

import config
from edge import OrderBookTop

_client: ClobClient | None = None


def get_client() -> ClobClient:
    global _client
    if _client is not None:
        return _client

    if not config.PRIVATE_KEY:
        raise RuntimeError("PK is not set in .env — cannot initialize the CLOB client.")

    if not (config.CLOB_API_KEY and config.CLOB_API_SECRET and config.CLOB_API_PASSPHRASE):
        raise RuntimeError(
            "CLOB_API_KEY/SECRET/PASSPHRASE are not set — run "
            "scripts/setup_clob_credentials.py once before trading."
        )

    _client = ClobClient(
        config.CLOB_API_BASE,
        key=config.PRIVATE_KEY,
        chain_id=config.CHAIN_ID,
        signature_type=config.SIGNATURE_TYPE,
        funder=config.FUNDER_ADDRESS or None,
        creds=ApiCreds(
            api_key=config.CLOB_API_KEY,
            api_secret=config.CLOB_API_SECRET,
            api_passphrase=config.CLOB_API_PASSPHRASE,
        ),
    )
    return _client


def get_book_top(up_token_id: str, down_token_id: str) -> OrderBookTop:
    client = get_client()
    books = client.get_order_books(
        [BookParams(token_id=up_token_id), BookParams(token_id=down_token_id)]
    )
    by_asset = {b.asset_id: b for b in books}
    up_book = by_asset[up_token_id]
    down_book = by_asset[down_token_id]

    def best(entries, pick_max: bool) -> tuple[float, float]:
        if not entries:
            return (0.0, 0.0)
        best_entry = max(entries, key=lambda e: float(e.price)) if pick_max else min(
            entries, key=lambda e: float(e.price)
        )
        return float(best_entry.price), float(best_entry.size)

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
    books = client.get_order_books([BookParams(token_id=token_id)])
    if not books:
        return 0.0
    min_size = getattr(books[0], "min_order_size", None)
    return float(min_size) if min_size else 0.0


def get_collateral_balance_usdc() -> float:
    client = get_client()
    result = client.get_balance_allowance(BalanceAllowanceParams(asset_type=AssetType.COLLATERAL))
    return float(result["balance"]) / 1e6  # collateral uses 6 decimals


def get_outcome_token_balance(token_id: str) -> float:
    """Direct on-chain-derived balance for one outcome token — used by the risk
    manager to confirm open-position state independent of local JSON state."""
    client = get_client()
    result = client.get_balance_allowance(
        BalanceAllowanceParams(asset_type=AssetType.CONDITIONAL, token_id=token_id)
    )
    return float(result["balance"]) / 1e6


@dataclass
class PlacedOrder:
    order_id: str
    raw_response: dict


def place_limit_buy(token_id: str, limit_price: float, stake_usdc: float) -> PlacedOrder:
    """Places a GTC limit buy. size is in shares; stake_usdc / limit_price gives
    the share count that costs exactly stake_usdc at that price."""
    client = get_client()
    size_shares = round(stake_usdc / limit_price, 2)
    order_args = OrderArgsV2(token_id=token_id, price=limit_price, size=size_shares, side=Side.BUY)
    resp = client.create_and_post_order(order_args=order_args, order_type=OrderType.GTC)
    return PlacedOrder(order_id=resp.get("orderID", ""), raw_response=resp)


def place_limit_sell(token_id: str, limit_price: float, size_shares: float) -> PlacedOrder:
    client = get_client()
    order_args = OrderArgsV2(token_id=token_id, price=limit_price, size=round(size_shares, 2), side=Side.SELL)
    resp = client.create_and_post_order(order_args=order_args, order_type=OrderType.GTC)
    return PlacedOrder(order_id=resp.get("orderID", ""), raw_response=resp)


def cancel_all_orders() -> None:
    get_client().cancel_all()
