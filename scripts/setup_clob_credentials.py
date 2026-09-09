"""One-time: derive CLOB API credentials from your wallet's private key.
Run this once, then copy the printed values into .env as CLOB_API_KEY/
CLOB_API_SECRET/CLOB_API_PASSPHRASE. Never commit them."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from py_clob_client_v2 import ClobClient

import config


def main() -> None:
    if not config.PRIVATE_KEY:
        sys.exit("Set PK in .env first.")

    client = ClobClient(
        config.CLOB_API_BASE,
        key=config.PRIVATE_KEY,
        chain_id=config.CHAIN_ID,
        signature_type=config.SIGNATURE_TYPE,
        funder=config.FUNDER_ADDRESS or None,
    )
    creds = client.create_or_derive_api_key()
    print("\nAdd these to your .env file (do not commit them):\n")
    print(f"CLOB_API_KEY={creds.api_key}")
    print(f"CLOB_API_SECRET={creds.api_secret}")
    print(f"CLOB_API_PASSPHRASE={creds.api_passphrase}")


if __name__ == "__main__":
    main()
