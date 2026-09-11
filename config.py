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
MAX_STAKE_PER_TRADE_USDC = 3.00  # was 5.00 -- per 2026-09-10 review: realized 55.6% win rate is
                                  # below the ~57-59% payout-implied breakeven; Kelly is ~zero
                                  # to slightly negative until calibration is re-verified on
                                  # more trades, so size for survival, not growth
DAILY_LOSS_LIMIT_USDC = 42.50  # 10% of starting bankroll
MAX_CONCURRENT_POSITIONS = 1
MIN_EDGE_BPS_TO_TRADE = 2400  # was 1500 -- sampled 1500-1900bps entries went 2/8, >=1900bps went 4/7;
                               # tune only via config.py, never at runtime
MAX_PRICE_SLIPPAGE_BPS = 200  # limit_price may not exceed the observed entry_price by more than this
PROBABILITY_SHRINKAGE_K = 0.40  # applied in quant_signal.py: p_used = 0.5 + K*(p_raw - 0.5).
                                  # Added per 2026-09-10 review: the quant signal is badly
                                  # overconfident (stated p averaged 0.731 on entries bought
                                  # against a realized 55.6%, an implied K of ~0.24) -- 0.40 is
                                  # a conservative middle setting pending a proper fit on more
                                  # closed trades with logged entry prices.
DAY_BOUNDARY_TZ = ZoneInfo("America/New_York")

# --- Timing ---
WINDOW_SECONDS = 300
FAST_TICK_SECONDS = 18       # cadence for the first FAST_PHASE_SECONDS of a window
FAST_PHASE_SECONDS = 105
SLOW_TICK_SECONDS = 35        # cadence for the remainder of the window
DISCOVERY_TIMEOUT_SECONDS = 8
# 12s was too tight: the Head-Trader model runs adaptive thinking (on by
# default when no `thinking` param is sent), so tail latency regularly exceeds
# it — and a timed-out call means a *skipped candidate edge*, which is the rare
# and valuable event this whole desk exists to catch. Retries are disabled for
# this call (see llm/client.py) so a slow call costs one timeout, not three;
# the tick loop's next pass ~18s later is the real retry.
LLM_CALL_TIMEOUT_SECONDS = 25

# --- Market discovery (Polymarket "Bitcoin Up or Down" 5-min series) ---
GAMMA_API_BASE = "https://gamma-api.polymarket.com"
MARKET_SLUG_PREFIX = "btc-updown-5m-"  # + window-start epoch seconds (UTC)

# --- Chain (Polygon mainnet, chain_id 137) ---
# Only used for the Chainlink cross-check read in price_feed.py — order
# placement/redemption go through polymarket-client's SecureClient, which
# manages its own RPC/relayer internally and needs none of this.
CHAIN_ID = 137
POLYGON_RPC_URL = os.environ.get("POLYGON_RPC_URL", "https://polygon-rpc.com")
CHAINLINK_BTCUSD_FEED_ADDRESS = "0xc907E116054Ad103354f2D350FD2514433D57F6f"  # Polygon mainnet, 8 decimals

# --- Wallet auth (from .env, never hardcoded) ---
# The private key alone is enough for order placement: SecureClient.create()
# derives the deposit wallet address and CLOB API credentials automatically,
# deploying the wallet on first use if needed. Redemption is a *gasless*
# transaction for a deposit-wallet account, though, and that specifically
# requires a Relayer (or Builder) API key — order placement works without it,
# redemption doesn't ("Gasless transactions require a Builder API Key or
# Relayer API Key", confirmed against a real account).
PRIVATE_KEY = os.environ.get("PK", "")
RELAYER_API_KEY = os.environ.get("RELAYER_API_KEY", "")
RELAYER_API_KEY_ADDRESS = os.environ.get("RELAYER_API_KEY_ADDRESS", "")

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
