"""Central configuration: risk limits, timing, and contract addresses.

Nothing here should be writable by an LLM at runtime — the Performance
Review agent can *suggest* changes to these values, but a human edits
this file, not the bot.

The risk numbers come from the active *risk profile* (see risk_profiles.py),
selected with the RISK_PROFILE environment variable and defaulting to
"standard". They're still exposed as plain module constants here, so every
consumer reads them the same way it always has and only the values change.
"""
import os
from pathlib import Path
from zoneinfo import ZoneInfo

import truststore
from dotenv import load_dotenv

import risk_profiles

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

# Which profile's numbers to run. "standard" is the conservative desk that was
# approved originally; "aggressive" is the HIGH RISK / HIGH REWARD profile. Read
# risk_profiles.py before switching — in particular the note that the desk's edge
# is currently unvalidated against the corrected settlement model, which is the
# whole reason the aggressive profile sizes by Kelly rather than by a flat bigger
# number. An unrecognised name raises at import rather than defaulting.
RISK_PROFILE_NAME = os.environ.get("RISK_PROFILE", "standard").strip().lower() or "standard"
RISK_PROFILE = risk_profiles.get(RISK_PROFILE_NAME)

MAX_STAKE_PER_TRADE_USDC = RISK_PROFILE.max_stake_per_trade_usdc
MIN_STAKE_USDC = RISK_PROFILE.min_stake_usdc
DAILY_LOSS_LIMIT_USDC = RISK_PROFILE.daily_loss_limit_usdc
MAX_PRICE_SLIPPAGE_BPS = RISK_PROFILE.max_price_slippage_bps  # limit_price may not exceed the observed entry_price by more than this
MIN_EDGE_BPS_TO_TRADE = RISK_PROFILE.min_edge_bps_to_trade
PROBABILITY_SHRINKAGE_K = RISK_PROFILE.probability_shrinkage_k  # quant_signal.py: p_used = 0.5 + K*(p_raw - 0.5)
MIN_LIQUIDITY_USDC = RISK_PROFILE.min_liquidity_usdc
KELLY_ENABLED = RISK_PROFILE.kelly_enabled
KELLY_MULTIPLIER = RISK_PROFILE.kelly_multiplier
KELLY_CAP_FRACTION = RISK_PROFILE.kelly_cap_fraction

# Not profile-controlled: risk_manager.py enforces this with a single position
# slot and raises at import if it isn't 1. Raising it needs that check rewritten
# to track a list first, or the cap is simply unenforced.
MAX_CONCURRENT_POSITIONS = 1

DAY_BOUNDARY_TZ = ZoneInfo("America/New_York")

# --- Timing ---
WINDOW_SECONDS = 300
FAST_TICK_SECONDS = 18       # cadence for the first FAST_PHASE_SECONDS of a window
FAST_PHASE_SECONDS = 105
SLOW_TICK_SECONDS = 35        # cadence for the remainder of the window
DISCOVERY_TIMEOUT_SECONDS = 8

# --- Settlement series modelling (see price_feed.py and quant_signal.py) ---
# Polymarket settles on the TWAP of Chainlink's BTC/USD *TWAP-60s* stream, so the
# series we model is a 60-second trailing average. Matching the stream's own
# window is the point; changing this makes our series a different shape from the
# one that decides the market.
SETTLEMENT_TWAP_LOOKBACK_SECONDS = 60
# The settlement reference is that trailing average *at the window boundary*,
# which can only be computed from history predating the window. So we start
# sampling before the boundary. Slightly more than the lookback, for full
# coverage even if a sample or two fails.
PREROLL_SECONDS = 75
PREROLL_TICK_SECONDS = 10
# Below this window-scale volatility the signal refuses to have an opinion. The
# probability is a ratio of accumulated area to remaining diffusion, so a σ near
# zero makes any nonzero area look like certainty — and a σ this low doesn't mean
# calm, it means the feed is stuck or quantised. Reference points: BTC's
# annualised vol implies a ~15bps 5-minute stdev, but a live 95-second sample on
# 2026-09-11 measured only 3.0bps in a quiet hour. 1bps is below both.
MIN_WINDOW_STDEV_BPS = 1.0
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
# Only used for the Chainlink level cross-check in price_feed.py — order
# placement/redemption go through polymarket-client's SecureClient, which
# manages its own RPC/relayer internally and needs none of this.
CHAIN_ID = 137
POLYGON_RPC_URL = os.environ.get("POLYGON_RPC_URL", "https://polygon-rpc.com")
# The on-chain BTC/USD aggregator. This is NOT the feed these markets settle on:
# settlement uses the Chainlink *Data Streams* BTC/USD TWAP-60s stream
# (data.chain.link/streams/btc-usd-twap-60s-streams), a different, off-chain,
# credentialed product. This aggregator is a spot-level sanity check only, and it
# only updates on a deviation/heartbeat trigger, so a read can be minutes stale.
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
# "low" is meant for subagents and simple classification; this is the single
# call that decides whether to commit real capital, so it runs at "medium".
# At 10-30 qualifying edges/day the extra thinking tokens cost cents. The real
# cost is latency, which is why LLM_CALL_TIMEOUT_SECONDS stays capped at 25s:
# a decision built on a 30-second-old order book is worse than no decision, and
# a timeout just defers to the next tick with a fresh book.
HEAD_TRADER_EFFORT = "medium"
PERFORMANCE_REVIEW_MODEL = "claude-opus-5"

# --- Safety ---
DRY_RUN = os.environ.get("DRY_RUN", "true").strip().lower() != "false"
KILL_SWITCH_FLAG_PATH = DATA_DIR / "kill_switch.flag"
TRADE_JOURNAL_PATH = DATA_DIR / "trade_journal.csv"
DAILY_STATE_PATH = DATA_DIR / "daily_state.json"
RISK_LOG_PATH = LOG_DIR / "risk_log.csv"
