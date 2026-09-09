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
from dataclasses import dataclass

from polymarket.auth import RelayerApiKey
from polymarket.clients.secure import SecureClient

import config
from edge import OrderBookTop

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
    status: str


def place_limit_buy(token_id: str, limit_price: float, stake_usdc: float) -> PlacedOrder:
    """Places a GTC limit buy. size is in shares; stake_usdc / limit_price gives
    the share count that costs exactly stake_usdc at that price."""
    client = get_client()
    size_shares = round(stake_usdc / limit_price, 2)
    resp = client.place_limit_order(token_id=token_id, price=limit_price, size=size_shares, side="BUY")
    if not resp.ok:
        raise RuntimeError(f"order rejected: {resp.code}: {resp.message}")
    return PlacedOrder(order_id=resp.order_id, status=resp.status)


def place_limit_sell(token_id: str, limit_price: float, size_shares: float) -> PlacedOrder:
    client = get_client()
    resp = client.place_limit_order(token_id=token_id, price=limit_price, size=round(size_shares, 2), side="SELL")
    if not resp.ok:
        raise RuntimeError(f"order rejected: {resp.code}: {resp.message}")
    return PlacedOrder(order_id=resp.order_id, status=resp.status)


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
