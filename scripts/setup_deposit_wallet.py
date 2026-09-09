"""One-time: derives your deposit wallet address from PK and deploys it if
it isn't already deployed (SecureClient.create() does this automatically —
this script just makes the one-time cost/confirmation explicit before you
start the bot, rather than having it happen silently on the first run).

Run this once, then deposit your collateral (pUSD/USDC) into the printed
address via the Polymarket website if you haven't already.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import clob_client
import config


def main() -> None:
    if not config.PRIVATE_KEY:
        sys.exit("Set PK in .env first.")

    print("Deriving and deploying (if needed) your deposit wallet...")
    address = clob_client.get_deposit_wallet_address()
    print(f"\nDeposit wallet address: {address}")
    print(
        "\nIf you haven't already, deposit your collateral into this exact address "
        "via the Polymarket website before running the smoke test."
    )


if __name__ == "__main__":
    main()
