"""Pre-launch checks: run this before ever setting DRY_RUN=false. Refuses to
run at all if DRY_RUN is already false, since this script also exercises
config/credentials that shouldn't be touched carelessly while live."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config


def check(label: str, fn) -> bool:
    try:
        result = fn()
        print(f"[PASS] {label}: {result}")
        return True
    except Exception as e:
        print(f"[FAIL] {label}: {e}")
        return False


def advisory(label: str, fn) -> bool:
    """Same as check(), but a failure doesn't block going live. Reserved for
    things outside the trading path — a smoke test that always fails on
    something optional is a smoke test whose failures stop being read."""
    try:
        result = fn()
        print(f"[PASS] {label}: {result}")
        return True
    except Exception as e:
        print(f"[WARN] {label}: {e}")
        return False


def main() -> None:
    if not config.DRY_RUN:
        sys.exit("DRY_RUN is false — refusing to run the smoke test. Set DRY_RUN=true in .env first.")

    ok = True

    def check_anthropic():
        from llm.client import get_client
        resp = get_client().messages.create(
            model=config.HEAD_TRADER_MODEL, max_tokens=16,
            messages=[{"role": "user", "content": "Reply with the single word: ok"}],
        )
        return next(b.text for b in resp.content if b.type == "text").strip()

    def check_clob_balance():
        import clob_client
        address = clob_client.get_deposit_wallet_address()
        balance = clob_client.get_collateral_balance_usdc()
        return (
            f"${balance:.2f} collateral at deposit wallet {address} "
            f"(confirm this matches what you deposited — Polymarket docs currently list "
            f"the collateral asset as pUSD, not USDC)"
        )

    def check_relayer_key():
        # Confirmed against a real account: order placement works without this,
        # but redemption is a gasless relay transaction that fails without it
        # ("Gasless transactions require a Builder API Key or Relayer API Key").
        # Without this, a won/lost position never clears from local state,
        # which blocks all future trades — not just a missed nice-to-have.
        if not (config.RELAYER_API_KEY and config.RELAYER_API_KEY_ADDRESS):
            raise RuntimeError(
                "RELAYER_API_KEY / RELAYER_API_KEY_ADDRESS not set in .env — "
                "redemption will fail and block all future trades once a position closes."
            )
        return f"configured for address {config.RELAYER_API_KEY_ADDRESS}"

    def check_market_discovery():
        from market_discovery import discover_market
        m = discover_market()
        return f"{m.slug} — {m.question}"

    def check_settlement_rule():
        """Confirms the live market still settles the way quant_signal models it
        (TWAP over the window vs. the price at its start). If Polymarket ever
        changes this wording, the whole signal is estimating the wrong quantity
        and there'd otherwise be nothing to tell us."""
        from market_discovery import discover_market, get_open_market_by_slug
        row = get_open_market_by_slug(discover_market().slug) or {}
        text = (row.get("description") or "").lower()
        missing = [
            phrase for phrase in ("time-weighted average price", "beginning of that range", "chainlink")
            if phrase not in text
        ]
        if missing:
            raise RuntimeError(
                f"resolution description no longer mentions {missing} — re-read it and re-check "
                "quant_signal.py's model before trading:\n" + (row.get("description") or "(empty)")
            )
        return "TWAP-over-window vs. window-start price, per Chainlink (unchanged)"

    def check_chainlink_feed():
        from price_feed import fetch_chainlink_btcusd
        price, updated_at = fetch_chainlink_btcusd()
        return f"${price:,.2f} (updated_at={updated_at})"

    ok &= check("Anthropic API reachable", check_anthropic)
    ok &= check("CLOB collateral balance / deposit wallet", check_clob_balance)
    ok &= check("Relayer API key (needed for redemption)", check_relayer_key)
    ok &= check("Market discovery (current window)", check_market_discovery)
    ok &= check("Settlement rule still matches the model", check_settlement_rule)
    # Advisory: the on-chain aggregator is a level cross-check, NOT the settlement
    # feed (settlement uses the credentialed Chainlink Data Streams TWAP-60s
    # stream), and nothing in the trading path reads it. Note the default public
    # polygon-rpc.com currently answers 401 — set POLYGON_RPC_URL to your own
    # endpoint if you want this cross-check to work.
    advisory("Chainlink BTC/USD on-chain aggregator (cross-check only)", check_chainlink_feed)

    print("\nAll checks passed." if ok else "\nOne or more checks FAILED — do not go live yet.")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
