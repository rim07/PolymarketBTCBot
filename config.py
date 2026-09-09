"""Central configuration: risk limits, timing, and contract addresses.

Nothing here should be writable by an LLM at runtime — the Performance
Review agent can *suggest* changes to these values, but a human edits
this file, not the bot.
"""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import truststore
from dotenv import load_dotenv

# Use the OS trust store (not just certifi's bundle) for all TLS verification.
# Needed on networks that do corporate TLS inspection (e.g. Zscaler) — Windows
# trusts the proxy's root CA, but Python's bundled certifi list doesn't by
# default, which otherwise breaks web3.py's RPC calls with a cert-verify error.
truststore.inject_into_ssl()

load_dotenv()

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
LOG_DIR = ROOT / "logs"
DATA_DIR.mkdir(exist_ok=True)
LOG_DIR.mkdir(exist_ok=True)

# --- Bankroll / risk limits (hard caps, enforced in risk_manager.py) ---
STARTING_BANKROLL_USDC = 425.00
MAX_STAKE_PER_TRADE_USDC = 5.00
DAILY_LOSS_LIMIT_USDC = 42.50  # 10% of starting bankroll
MAX_CONCURRENT_POSITIONS = 1
MIN_EDGE_BPS_TO_TRADE = 1500  # 15 cents on a $1 outcome; tune only via config.py, never at runtime
DAY_BOUNDARY_TZ = ZoneInfo("America/New_York")

# --- Timing ---
WINDOW_SECONDS = 300
FAST_TICK_SECONDS = 18       # cadence for the first FAST_PHASE_SECONDS of a window
FAST_PHASE_SECONDS = 105
SLOW_TICK_SECONDS = 35        # cadence for the remainder of the window
DISCOVERY_TIMEOUT_SECONDS = 8
LLM_CALL_TIMEOUT_SECONDS = 12

# --- Market discovery (Polymarket "Bitcoin Up or Down" 5-min series) ---
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
CLOB_API_BASE = "https://clob.polymarket.com"
MARKET_SLUG_PREFIX = "btc-updown-5m-"  # + window-start epoch seconds (UTC)

# --- Chain / contracts (Polygon mainnet, chain_id 137) ---
CHAIN_ID = 137
POLYGON_RPC_URL = os.environ.get("POLYGON_RPC_URL", "https://polygon-rpc.com")
CTF_CONTRACT_ADDRESS = "0x4D97DCd97eC945f40cF65F87097ACe5EA0476045"
COLLATERAL_TOKEN_ADDRESS = "0xC011a7E12a19f7B1f670d46F03B03f3342E82DFB"  # pUSD per Polymarket docs (2026) — confirm vs. your wallet's asset during smoke test
CHAINLINK_BTCUSD_FEED_ADDRESS = "0xc907E116054Ad103354f2D350FD2514433D57F6f"  # Polygon mainnet, 8 decimals

# --- Wallet / CLOB auth (from .env, never hardcoded) ---
PRIVATE_KEY = os.environ.get("PK", "")
SIGNATURE_TYPE = int(os.environ.get("SIGNATURE_TYPE", "1"))
FUNDER_ADDRESS = os.environ.get("FUNDER", "")
CLOB_API_KEY = os.environ.get("CLOB_API_KEY", "")
CLOB_API_SECRET = os.environ.get("CLOB_API_SECRET", "")
CLOB_API_PASSPHRASE = os.environ.get("CLOB_API_PASSPHRASE", "")

# --- Anthropic ---
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
HEAD_TRADER_MODEL = "claude-sonnet-5"
PERFORMANCE_REVIEW_MODEL = "claude-opus-5"

# --- Safety ---
DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() != "false"
KILL_SWITCH_FLAG_PATH = DATA_DIR / "kill_switch.flag"
TRADE_JOURNAL_PATH = DATA_DIR / "trade_journal.csv"
DAILY_STATE_PATH = DATA_DIR / "daily_state.json"
RISK_LOG_PATH = LOG_DIR / "risk_log.csv"
