"""Risk profiles: how aggressive the desk is, chosen once at startup.

Set with the RISK_PROFILE environment variable (default "standard"):

    RISK_PROFILE=aggressive    # HIGH RISK / HIGH REWARD

config.py resolves the profile into the same module-level constants the rest of
the code already reads (MAX_STAKE_PER_TRADE_USDC, MIN_EDGE_BPS_TO_TRADE, ...),
so a profile changes those *values* and nothing else. In particular the profile
cannot bypass a single check in risk_manager.py — the caps stay absolute, the
profile only decides what they are.

**Read this before switching to "aggressive".** The 2026-09-10 review measured a
55.6% realized win rate against a ~57-59% payout-implied breakeven, and
PROBABILITY_SHRINKAGE_K / MIN_EDGE_BPS_TO_TRADE were both fitted against the old
close-vs-open estimator that quant_signal.py no longer uses. So the desk's edge
is currently *unvalidated*, and on a negative edge more size and more trades
lose money faster — variance does not become profit. That is the reason the
aggressive profile sizes by the Kelly criterion (below) rather than by a flat
larger number: Kelly scales with the edge it is given and goes to zero when
there isn't one, so the profile is aggressive where there's something to be
aggressive about and quiet where there isn't. Run a day on "aggressive" with
DRY_RUN=true and read scripts/review_daily.py's per-profile breakdown before
committing real size to it.

Two design rules worth keeping if you add or tune a profile:

1. **Stake and the daily halt move together.** A profile that can lose
   `max_stake` per trade against a `daily_loss_limit` halts after
   `limit / stake` full losses. Push stake up without pushing the limit up and
   the desk simply stops trading earlier in the day — that caps the upside the
   bigger stake was supposed to buy. Both profiles below keep the ratio near 10
   losing trades, which is roughly a 1-in-1000 run of bad luck at a 50% hit rate
   (see `losing_trades_to_halt`).
2. **Nothing here raises MAX_CONCURRENT_POSITIONS.** It stays 1 in every profile
   because risk_manager.py enforces it with a single position slot and raises at
   import if the value isn't 1. Holding several windows at once is a real
   aggression lever, but it needs that check rewritten to track a list first.
3. **`min_edge_bps_to_trade` and `probability_shrinkage_k` are not independent,
   and together they decide what *prices* the desk can trade.** Shrinkage caps
   p_side at `0.5 + K/2`, and edge.py requires `p_side - ask >= min_edge`, so the
   highest price the desk can ever pay is `0.5 + K/2 - min_edge`
   (`max_reachable_entry_price`). This has already gone wrong once: at K=0.40 and
   2400bps the standard profile could not buy anything above **0.46** whatever
   the model believed, and a flat signal capped it at 0.26 — so a threshold
   raised for safety silently converted the desk into a deep-underdog buyer,
   which is the losing pattern edge.py's side-selection guard was added to stop.
   Raising `min_edge_bps_to_trade` is therefore *not* a purely conservative
   change. Check `max_reachable_entry_price` after touching either number;
   test_risk_profiles.py asserts it stays at or above 0.50 for every profile.
"""
from dataclasses import dataclass


@dataclass(frozen=True)
class RiskProfile:
    name: str

    # --- hard caps (enforced in risk_manager.py) ---
    max_stake_per_trade_usdc: float
    daily_loss_limit_usdc: float
    max_price_slippage_bps: int
    # Floor below which a trade isn't worth placing. Kelly sizing is a fraction
    # of live bankroll, so it degrades gracefully all the way down to dust: on a
    # drained wallet it will happily ask for a $0.04 order, which occupies the
    # desk's single position slot for a whole window in exchange for nothing.
    # Without this the only thing standing in the way is whatever minimum the
    # exchange happens to report, and get_min_order_size can legitimately come
    # back as 0.
    min_stake_usdc: float

    # --- entry selectivity ---
    min_edge_bps_to_trade: int
    probability_shrinkage_k: float
    # Conviction floor: the model's own (already shrunk) probability for the side
    # being bought. Independent of price, and that independence is the point.
    # min_edge_bps_to_trade alone tests `p_side - ask`, which a cheap enough ask
    # satisfies no matter how little the model knows — a p_side of 0.504 clears a
    # 2400bps threshold against any ask at or below 0.26. So the desk would buy
    # near-worthless contracts on a coin-flip signal and call it a 3000bps edge.
    # This gate says: don't trade at all without an actual directional view.
    min_model_p_side: float
    # Hard floor on the entry price, as a backstop below the two gates above.
    # Late in a 5-minute window the market's price is set by participants reading
    # the real Chainlink settlement stream, while this desk reconstructs it from
    # Binance spot — so a contract the market has marked at 0.05 is one we are in
    # no position to contradict. A big `p_side - ask` number there is a statement
    # about our model, not about free money.
    min_entry_price: float
    # Top-of-book notional an entry must have available before edge.py will call
    # it tradeable. A floor, not the intended stake — risk_manager separately
    # caps the stake at the depth actually showing, so a thin book shrinks the
    # trade instead of walking up the order book to fill it.
    min_liquidity_usdc: float

    # --- sizing ---
    # Kelly sizing maximises the expected growth rate of the bankroll, which is
    # the formal version of "maximise profits". For a binary contract bought at
    # `price` with win probability `p`, the growth-optimal fraction of bankroll
    # is p - (1-p)*price/(1-price) (see kelly_fraction below). When it's <= 0
    # there is no growth-positive bet and the trade is rejected outright.
    kelly_enabled: bool
    # 1.0 = full Kelly; < 1 damps estimation error. Worth checking against the
    # per-trade dollar cap when you tune it: if multiplier * f* * bankroll clears
    # max_stake_per_trade_usdc across the whole plausible range of f*, every
    # trade sizes to the cap and Kelly stops distinguishing anything.
    kelly_multiplier: float
    kelly_cap_fraction: float    # hard ceiling as a fraction of live bankroll

    # --- judgment ---
    # Injected into the Head-Trader system prompt. The profile has to reach the
    # LLM too: a model told to "prefer SKIP over a marginal trade" will veto
    # exactly the marginal-but-positive-expectancy trades an aggressive profile
    # exists to take, and the risk manager can only ever reject, never overrule
    # a SKIP into a trade.
    posture: str

    @property
    def losing_trades_to_halt(self) -> float:
        """Full-stake losses the daily limit absorbs before trading halts."""
        if self.max_stake_per_trade_usdc <= 0:
            return 0.0
        return self.daily_loss_limit_usdc / self.max_stake_per_trade_usdc

    @property
    def shrunk_probability_ceiling(self) -> float:
        """The most confident p_side this profile's shrinkage can ever emit.

        quant_signal applies p = 0.5 + K*(p_raw - 0.5), so even a raw probability
        of 1.0 comes out at 0.5 + K/2. Every entry gate below is measured against
        this ceiling, not against 1.0.
        """
        return 0.5 + self.probability_shrinkage_k / 2.0

    @property
    def max_reachable_entry_price(self) -> float:
        """The highest price this profile can ever pay, given its own numbers.

        edge.py requires `p_side - ask >= min_edge`, and p_side can't exceed the
        shrinkage ceiling, so the threshold silently caps the entry price too.
        This is the coupling that broke the desk once already (see design rule 3)
        and it is easy to reintroduce, because the two parameters look
        independent and live on different lines.
        """
        return self.shrunk_probability_ceiling - self.min_edge_bps_to_trade / 10_000


STANDARD = RiskProfile(
    name="standard",
    # Per the 2026-09-10 review: a 55.6% realized win rate is below the ~57-59%
    # payout-implied breakeven, so Kelly is ~zero to slightly negative until
    # calibration is re-verified. Size for survival, not growth.
    max_stake_per_trade_usdc=3.00,
    daily_loss_limit_usdc=42.50,      # 10% of the $425 starting bankroll
    max_price_slippage_bps=200,
    min_stake_usdc=1.00,
    # Was 2400, which combined with K=0.40 to cap the entry price at 0.46 and
    # confine the desk to deep underdogs (design rule 3 above). 800bps is a
    # post-shrinkage margin: shrinkage has *already* discounted the model's
    # overconfidence, and demanding another 24 points on top of it double-counted
    # the same correction until the only trades left were the ones the market had
    # written off. 8 points of edge on a shrunk probability, plus a conviction
    # floor and a price floor, is the selectivity — not a single large number.
    min_edge_bps_to_trade=800,
    # The quant signal is badly overconfident (stated p averaged 0.731 on entries
    # bought against a realized 55.6%, an implied K of ~0.24). 0.40 is a
    # conservative middle setting pending a proper fit on more closed trades.
    probability_shrinkage_k=0.40,
    # Requires a raw p_up of >= 0.70 before shrinkage. Comfortably inside what
    # the signal produces (it averaged 0.731 raw on entries), so this filters the
    # no-information ticks rather than the trades.
    min_model_p_side=0.58,
    # 0.25 caps the payout at 4x. Every entry below this on the reviewed day was
    # a loss, and the mechanism is not bad luck: a 5-minute window priced under
    # 0.25 is usually one the settlement TWAP has largely decided, and our proxy
    # of that TWAP is the weaker read of the two.
    min_entry_price=0.25,
    min_liquidity_usdc=3.00,          # == max stake: preserves the original all-or-nothing depth gate
    kelly_enabled=False,              # flat sizing at the cap, as originally approved
    kelly_multiplier=0.0,
    kelly_cap_fraction=0.0,
    posture=(
        "- Prefer SKIP over a marginal trade. This desk's edge comes from selectivity, not volume."
    ),
)

AGGRESSIVE = RiskProfile(
    name="aggressive",
    # 4x the standard cap and ~2.8% of a $425 bankroll. The Kelly fraction below
    # is usually the binding constraint on a weak edge; this is the ceiling on a
    # strong one.
    max_stake_per_trade_usdc=12.00,
    # 30% of the $425 starting bankroll — deliberately steep, and the number to
    # cut first if this profile turns out to be trading a negative edge. Kept at
    # ~10.6x max_stake so a normal run of losses doesn't halt the day early (see
    # design rule 1 in the module docstring).
    daily_loss_limit_usdc=127.50,
    # Wider, because an edge worth 1200bps survives paying 400bps to get in.
    # Still a hard bound: it's what stops the desk paying any price at all for a
    # position it has already talked itself into.
    max_price_slippage_bps=400,
    # Same floor as standard. Kelly will size below this on a weak edge or a
    # drawn-down bankroll, and when it does, not trading is the correct answer —
    # the position slot is worth more than a $0.40 punt.
    min_stake_usdc=1.00,
    # Below standard's 800. This is the single biggest driver of trade count, and
    # therefore of how fast a real edge compounds — or a negative one bleeds.
    # Thin edges get Kelly-sized down here rather than skipped, which is what
    # makes a low threshold tolerable in this profile and not in standard.
    min_edge_bps_to_trade=500,
    # Trust the model further out from 0.5. Note this compounds with the lower
    # edge threshold: less shrinkage widens every |p - 0.5|, which inflates
    # edge_bps as well, so the two together are a large loosening rather than two
    # small ones.
    probability_shrinkage_k=0.65,
    # Looser than standard's 0.58 — a raw p_up of ~0.58 rather than 0.70. Still a
    # real directional view: this profile is meant to take marginal *edges*, not
    # to trade on no information at all, and no amount of Kelly damping rescues a
    # position entered on a coin flip.
    min_model_p_side=0.55,
    # Lower than standard, so this profile can buy the genuine 4-6x longshots
    # standard declines. Not zero: below ~0.15 the market is reading the real
    # settlement stream and we are reading a proxy of it.
    min_entry_price=0.15,
    # Lower than standard so a thin top-of-book shrinks the trade instead of
    # cancelling it — at 12.00 the standard "depth must cover the full stake"
    # gate would silently refuse almost every window in this market.
    min_liquidity_usdc=2.00,
    kelly_enabled=True,
    # Tenth Kelly, and the multiplier is doing real work rather than damping for
    # its own sake. Full Kelly on this market asks for 10-70% of bankroll, all of
    # which is far above the $12 per-trade cap — so at multiplier 1.0 every
    # qualifying edge, thin or strong, came out at exactly $12 and the sizing rule
    # collapsed into a flat stake with extra steps (measured: p_side 0.55 @ 0.50
    # and p_side 0.82 @ 0.35 both sized to $12.00). At 0.10 the stake hits the cap
    # at f* = 12/(425*0.10) = 0.28, so the range that actually gets graduated is
    # f* 0.02-0.28 -> roughly $1-$12, and stronger edges sit at the cap. The
    # damping is independently correct anyway: fractional Kelly is the standard
    # response to an uncertain probability estimate, and this desk's estimate is
    # explicitly unvalidated against the corrected settlement model.
    kelly_multiplier=0.10,
    # Backstop, not the usual binding constraint: on a $425 bankroll the $12
    # absolute cap binds first. It exists to bound a pathological f* and to
    # de-leverage automatically as the bankroll draws down.
    kelly_cap_fraction=0.08,
    posture=(
        "- This desk is running its HIGH RISK / HIGH REWARD profile. It is deliberately trading a "
        "wider set of edges at larger size to maximise growth, and accepts a much larger drawdown "
        "to do it.\n"
        "- Do NOT skip a trade merely because it is marginal, or because the outcome is genuinely "
        "uncertain. A positive-expectancy bet is worth taking here even though it will often lose. "
        "Reserve SKIP for edges you think are actually *wrong*: a degraded or stale settlement "
        "reference, a signal the TWAP contradicts, an entry priced past the point where the edge "
        "survives, or a risk limit already breached.\n"
        "- Position size is set by a Kelly criterion on the model's own probability, so a weaker "
        "edge is automatically sized down instead of being skipped. Your stake_usdc figure is "
        "advisory only in this profile — size is not the lever you are being asked to pull.\n"
        "- Prefer hold_to_resolution unless the current bid is already close to the model's "
        "probability. Taking profit early caps the full $1 payout the position was sized for, and "
        "that payout is where this profile's return comes from."
    ),
)

PROFILES = {p.name: p for p in (STANDARD, AGGRESSIVE)}


def get(name: str) -> RiskProfile:
    """Resolve a profile by name, raising on anything unrecognised.

    Deliberately not tolerant: silently falling back to a default would mean a
    typo in RISK_PROFILE decides how much real money each trade risks, in
    whichever direction the typo happens to land.
    """
    try:
        return PROFILES[name]
    except KeyError:
        raise ValueError(
            f"Unknown RISK_PROFILE {name!r}. Valid profiles: {', '.join(sorted(PROFILES))}."
        ) from None


def kelly_fraction(p_win: float, price: float) -> float:
    """Growth-optimal fraction of bankroll to stake on a binary contract.

    A contract costing `price` pays $1 if it resolves your way, so the net odds
    on the stake are b = (1 - price)/price and the Kelly fraction is

        f* = (p*b - (1-p)) / b = p - (1-p)*price/(1-price)

    Returns 0.0 when the bet isn't growth-positive — which includes the case
    this desk most needs to handle: a probability that doesn't actually beat the
    price it's being asked to pay. Never returns a negative fraction; buying the
    other side is edge.py's decision, not a sizing question.
    """
    if not (0.0 < price < 1.0) or not (0.0 <= p_win <= 1.0):
        return 0.0
    f = p_win - (1.0 - p_win) * price / (1.0 - price)
    return max(f, 0.0)


def stake_fraction(profile: RiskProfile, p_win: float, price: float) -> float:
    """kelly_fraction() with the profile's multiplier and hard fraction cap."""
    if not profile.kelly_enabled:
        return 0.0
    return min(kelly_fraction(p_win, price) * profile.kelly_multiplier, profile.kelly_cap_fraction)
