"""Periodic sweep: for the tracked open position, once its window has
resolved, redeem winnings on-chain via the CTF contract's redeemPositions —
py-clob-client has no redemption method; this is a direct Polygon
transaction and costs gas in POL/MATIC (separate from the USDC/pUSD
bankroll — confirm this is funded before going live).
"""
import json
import sys
import time
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from web3 import Web3

import config
import journal
import market_discovery
import state

_CTF_ABI = [
    {
        "inputs": [
            {"name": "collateralToken", "type": "address"},
            {"name": "parentCollectionId", "type": "bytes32"},
            {"name": "conditionId", "type": "bytes32"},
            {"name": "indexSets", "type": "uint256[]"},
        ],
        "name": "redeemPositions",
        "outputs": [],
        "stateMutability": "nonpayable",
        "type": "function",
    }
]

_ZERO_BYTES32_HEX = "00" * 32


def _resolution_status(market_slug: str) -> tuple[bool, Optional[str]]:
    """Returns (is_resolved, winning_side). winning_side is "UP"/"DOWN"/None.
    A market is only treated as resolved once outcomePrices has snapped to a
    clean 0/1 — "closed": true alone can lag the actual on-chain settlement."""
    row = market_discovery.get_market_by_slug(market_slug)
    if not row or not row.get("closed"):
        return False, None
    outcomes = row["outcomes"]
    if isinstance(outcomes, str):
        outcomes = json.loads(outcomes)
    prices = row["outcomePrices"]
    if isinstance(prices, str):
        prices = json.loads(prices)
    prices = [float(p) for p in prices]
    up_idx, down_idx = outcomes.index("Up"), outcomes.index("Down")
    if prices[up_idx] >= 0.99:
        return True, "UP"
    if prices[down_idx] >= 0.99:
        return True, "DOWN"
    return False, None


def _redeem_on_chain(condition_id: str) -> str:
    w3 = Web3(Web3.HTTPProvider(config.POLYGON_RPC_URL))
    ctf = w3.eth.contract(address=Web3.to_checksum_address(config.CTF_CONTRACT_ADDRESS), abi=_CTF_ABI)
    account = w3.eth.account.from_key(config.PRIVATE_KEY)

    tx = ctf.functions.redeemPositions(
        Web3.to_checksum_address(config.COLLATERAL_TOKEN_ADDRESS),
        bytes.fromhex(_ZERO_BYTES32_HEX),
        bytes.fromhex(condition_id[2:] if condition_id.startswith("0x") else condition_id),
        [1, 2],
    ).build_transaction({
        "from": account.address,
        "nonce": w3.eth.get_transaction_count(account.address),
        "chainId": config.CHAIN_ID,
        "gasPrice": w3.eth.gas_price,
    })
    tx["gas"] = w3.eth.estimate_gas(tx)
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
    if receipt.status != 1:
        raise RuntimeError(f"redeemPositions transaction reverted (hash={tx_hash.hex()})")
    return tx_hash.hex()


def sweep() -> None:
    daily_state = state.ensure_current_day(state.load())
    position = daily_state.open_position
    if position is None:
        return

    is_resolved, winning_side = _resolution_status(position.market_slug)
    if not is_resolved:
        return

    won = winning_side == position.side
    payout_usdc = position.size_shares if won else 0.0
    pnl = payout_usdc - position.stake_usdc

    if position.is_dry_run:
        # Keyed off the position's own flag, not config.DRY_RUN — a dry-run
        # position must simulate its close even if the process has since
        # switched to live, since no real order was ever placed for it.
        journal.write_trade_row({
            "marketName": position.market_slug, "action": "DryRunRedeem",
            "usdcAmount": f"{payout_usdc:.6f}", "tokenAmount": f"{position.size_shares:.6f}",
            "tokenName": position.side, "decision_rationale": f"won={won} pnl={pnl:.2f}",
        })
        state.record_close(daily_state, realized_pnl_usdc=pnl)
        return

    row = market_discovery.get_market_by_slug(position.market_slug)
    if not row:
        return  # retry on the next sweep

    tx_hash = _redeem_on_chain(row["conditionId"])

    journal.write_trade_row({
        "marketName": position.market_slug, "action": "Redeem",
        "usdcAmount": f"{payout_usdc:.6f}", "tokenAmount": f"{position.size_shares:.6f}",
        "tokenName": position.side, "hash": tx_hash,
        "decision_rationale": f"won={won} pnl={pnl:.2f}",
    })
    state.record_close(daily_state, realized_pnl_usdc=pnl)


if __name__ == "__main__":
    # Standalone catch-up mode only — main.py calls sweep() itself once per window
    # from within its own process. Don't run this loop at the same time as main.py;
    # both would read-modify-write data/daily_state.json without file locking.
    while True:
        try:
            sweep()
        except Exception as e:
            print(f"redeem sweep error: {e}", file=sys.stderr)
        time.sleep(60)
