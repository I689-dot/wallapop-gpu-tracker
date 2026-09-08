"""Central configuration. Everything tunable lives here or in the environment."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

try:  # optional: handy locally, absent on CI is fine
    from dotenv import load_dotenv

    # .env.local wins over .env (load_dotenv never overrides what's already set),
    # and both lose to real environment variables — which is what GitHub Actions
    # injects from Secrets.
    load_dotenv(".env.local")
    load_dotenv()
except ImportError:  # pragma: no cover
    pass


def _f(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw not in (None, "") else default


def _i(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw not in (None, "") else default


# --------------------------------------------------------------- secrets
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

# --------------------------------------------------------------- geography
LATITUDE = _f("WP_LATITUDE", 40.44)          # Madrid 28020
LONGITUDE = _f("WP_LONGITUDE", -3.70)
DEFAULT_DISTANCE_KM = _i("WP_DEFAULT_DISTANCE_KM", 100)

# Wallapop's top-level "Tecnología y electrónica" tree. Kept because the
# searches table stores it, but note: verified live 2026-08-02, the API
# *silently ignores* category_ids — a request for 10304 still returns gaming
# laptops. Category filtering only works client-side, off each item's
# `taxonomy` array. See TAXONOMY_* below.
CATEGORY_GPU = "24200"

# ------------------------------------------------------------ taxonomy leaves
# Read off /api/v3/categories (verified live 2026-08-02).
TAXONOMY_COMPONENTS = 10304          # "Componentes y piezas de ordenador"
# Whole-machine categories. A GPU inside one of these is not a loose card, so
# its price is not a comp for one.
#
# Deliberately NOT used to suppress alerts: a card inside a PC is still a
# perfectly good buy, and the MAX_ALERT_PRICE cap below is what keeps whole
# machines out of the alert feed. This set exists purely to keep whole-machine
# prices out of the reference-price pool, which is the number that decides
# every ceiling.
# Matched against the taxonomy's *terminal* node only — see
# wallapop_client.Item.whole_machine, which is where that changed and why.
#
# 24115 ("PC gaming y streaming") is a branch as well as a destination: a
# listing may stop there, or carry on to a leaf below it. Both real shapes turn
# up live — (24200, 24203, 24115, 24117) is a prebuilt and
# (24200, 24203, 24115, 24130) is a loose graphics card sitting in the same
# section of the tree.
TAXONOMY_WHOLE_MACHINE = frozenset({
    24115,   # PC gaming y streaming
    24116,   # Portátiles gaming
    24117,   # Ordenadores sobremesa gaming
    10309,   # Ordenadores de sobremesa
    10310,   # Portátiles
})


# --------------------------------------------------------------- fee model
@dataclass(frozen=True)
class FeeModel:
    """Cost structure of one flip.

    net = ref_price*(1-seller_fee) - [buy*(1+buyer_fee) + buyer_fixed + shipping_in]
    """

    seller_fee: float
    buyer_fee: float
    buyer_fixed: float
    shipping_in: float
    target_margin: float
    label: str

    def required_margin(self, ref_price: float) -> float:
        """What this flip has to clear, in euros.

        A flat target scales badly. TARGET_MARGIN=50 is a 25% return on a
        200 EUR card and an 8% one on a 620 EUR card — the same rule that is
        demanding at the cheap end is nearly free at the expensive end, for the
        same work and the same capital at risk.

        So the requirement is whichever is greater: the flat floor, or a
        percentage of what the item is worth.
        """
        return max(self.target_margin, MARGIN_RATE * ref_price)

    def buy_ceiling(self, ref_price: float) -> float:
        """Max purchase price that still clears the required margin."""
        gross = ref_price * (1 - self.seller_fee)
        return (gross - self.buyer_fixed - self.shipping_in - self.required_margin(ref_price)) / (
            1 + self.buyer_fee
        )

    def net_margin(self, buy_price: float, ref_price: float) -> float:
        """Expected profit if bought at buy_price and resold at ref_price."""
        revenue = ref_price * (1 - self.seller_fee)
        cost = buy_price * (1 + self.buyer_fee) + self.buyer_fixed + self.shipping_in
        return revenue - cost


TARGET_MARGIN = _f("TARGET_MARGIN", 50.0)

# Minimum return as a fraction of what the item is worth, applied alongside the
# flat TARGET_MARGIN floor — whichever demands more wins. Without this the flat
# floor silently becomes a rounding error on expensive stock.
#
# The crossover is TARGET_MARGIN / MARGIN_RATE, so at the defaults anything
# with a reference under ~278 EUR is still governed by the 50 EUR floor exactly
# as before. Above it the rate binds, which tightens the feed for mid and
# high-tier cards: a 4070 at ref 330 must clear 59 EUR rather than 50, and a
# 4080 at ref 620 must clear 112. Set MARGIN_RATE=0 to restore the old
# flat-only behaviour.
MARGIN_RATE = _f("MARGIN_RATE", 0.18)

# Extra margin demanded while a model is still priced from its hand-written
# seed rather than from real reserved/sold comps. A seed is an educated guess
# at what something is worth, and the whole feed is only as good as that guess:
# if a seed is 25% too high, every ordinary listing looks like a bargain and the
# alert stream becomes noise. Demanding more while confidence is low is the
# honest way to express that, rather than quietly tolerating a week of false
# positives.
#
# The penalty is released when observed evidence outweighs the prior it is being
# shrunk toward — i.e. once the trimmed pool's total weight exceeds PRIOR_WEIGHT
# — NOT simply once MIN_COMPS comps exist. The distinction matters and used to
# be wrong: reaching MIN_COMPS only means a reference could be computed, and the
# first such reference is still mostly the seed (at PRIOR_WEIGHT=5 against a
# thin, time-decayed pool the seed is around 59% of the number). Dropping the
# penalty there switched off the defence against a bad seed at precisely the
# moment it was still guarding a mostly-seed price.
SEED_MARGIN_MULTIPLIER = _f("SEED_MARGIN_MULTIPLIER", 1.6)

# Default: shipped both ways — the worst case, and the gate we alert on.
# seller_fee=0 because Wallapop charges the seller nothing; only the buyer pays
# the protection fee (buyer_fee + buyer_fixed) when a shipped sale is protected.
SHIPPED = FeeModel(
    seller_fee=_f("SELLER_FEE", 0.0),
    buyer_fee=_f("BUYER_FEE", 0.075),
    buyer_fixed=_f("BUYER_FIXED", 0.69),
    shipping_in=_f("SHIPPING_IN", 4.50),
    target_margin=TARGET_MARGIN,
    label="shipped",
)

# Madrid in-person preset: no Wallapop fees, no shipping.
IN_PERSON = FeeModel(
    seller_fee=0.0,
    buyer_fee=0.0,
    buyer_fixed=0.0,
    shipping_in=0.0,
    target_margin=TARGET_MARGIN,
    label="in person",
)

# ------------------------------------------------------------- item condition
# Wallapop returns a structured condition on every listing
# (`type_attributes.condition.value`). Full enum, verified live 2026-08-02:
#
#   un_opened · in_box · new · as_good_as_new · good · fair · has_given_it_all
#
# Only the bottom tier is blocked. Everything else — including `fair` — stays:
# a card with a cosmetic flaw that still works is a legitimate flip, and the
# whole point of buying secondhand is tolerating wear the seller discounted for.
BLOCKED_CONDITIONS = frozenset(
    c.strip()
    for c in os.getenv("BLOCKED_CONDITIONS", "has_given_it_all").split(",")
    if c.strip()
)

# Sent to the API as a server-side pre-filter so blocked stock never even
# travels. The client still re-checks locally — the param is advisory and the
# search response does not echo the condition back.
ALLOWED_CONDITIONS = "un_opened,in_box,new,as_good_as_new,good,fair"

# ---------------------------------------------------------------- sellers
# Wallapop seller ids that may never trigger an alert. The same replica or
# empty-box listing reappears under a fresh item_id every few days, which
# defeats the (item_id, price) dedup entirely — the seller is the only stable
# identifier across those relistings. Comma-separated, empty by default.
BLOCKED_SELLERS = frozenset(
    s.strip() for s in os.getenv("BLOCKED_SELLERS", "").split(",") if s.strip()
)

# --------------------------------------------------------------- search shape
# `order_by=newest` on its own cripples the result set — the API returns a
# heavily truncated page (measured: 13 items bare, as few as 1 once a geo and
# category filter are added) rather than a full one. Pairing it with any valid
# `time_filter` restores a full 40-item page. Measured live 2026-08-02 on the
# alert loop's exact shape, 2 pages: 34 items -> 80.
#
# Valid values are today / lastWeek / lastMonth; anything else is a 400.
ALERT_TIME_FILTER = os.getenv("ALERT_TIME_FILTER", "lastWeek")

# Comps send one too, and it is not optional.
#
# This was briefly left empty on the strength of a local measurement — from a
# Spanish IP, most_relevance returns full pages either way, and adding a time
# filter cost 9% of the distribution (183 items over 5 pages vs 166). That
# measurement was worthless, because it was taken from the wrong place.
#
# `nationwide=True` deliberately sends no lat/lon, so the server geolocates the
# request by IP. From a GitHub Actions runner that resolves outside Spain, and
# a filterless query then comes back 200 OK with a well-formed envelope and an
# empty item list. The comps loop logged "0 items" on all 40 searches, every
# hour, for as long as run_log goes back — a total failure that looked exactly
# like a quiet market.
#
# A time_filter is what makes the query return results from that IP; it is the
# only reason the alert loop kept working. 9% less depth is the correct trade
# against 100% less data.
COMPS_TIME_FILTER = os.getenv("COMPS_TIME_FILTER", "lastWeek") or None

# Sort order for comps searches. `most_relevance` is the intuitive choice for a
# comps pool and it is the one that must never be used from CI.
#
# Measured 2026-08-17 against the live API, both loops on the same runner within
# the same minute, both nationwide with no lat/lon:
#
#   alert  order_by=newest         time_filter=lastWeek   -> 80 items / search
#   comps  order_by=most_relevance time_filter=lastMonth  ->  0 items / search
#
# Identical IP, identical endpoint, identical credentials. Every comps search
# returned HTTP 200 with a well-formed but empty `organic_search_results`
# section. From a Spanish IP the same comps request returns a full 40-item page,
# so the query is valid — relevance ranking simply resolves to nothing when the
# server cannot place the caller inside the marketplace, while recency ordering
# does not need that context.
#
# The earlier `time_filter` work fixed page size and was necessary, but it was
# never sufficient: it could not have fixed this, and the comps pool has been
# empty for as long as run_log goes back. Every reference price the bot has been
# quoting is therefore still its hand-written seed.
#
# lastWeek rather than lastMonth for the same reason — it is the exact pair
# proven to work above, and one variable at a time. The trailing pool is not
# narrowed by this: observations accumulate in the database run after run, so a
# 30-day COMPS_WINDOW_DAYS still fills from a 7-day search window. Widen only
# after confirming a run comes back non-empty.
COMPS_ORDER_BY = os.getenv("COMPS_ORDER_BY", "newest")

# --------------------------------------------------------------- comps math
MIN_COMPS = _i("MIN_COMPS", 5)
COMPS_WINDOW_DAYS = _i("COMPS_WINDOW_DAYS", 60)
TRIM_FRACTION = _f("TRIM_FRACTION", 0.10)

# Comps age out smoothly rather than falling off a 30-day cliff: a comp's
# weight halves every COMPS_HALFLIFE_DAYS. This is what lets COMPS_WINDOW_DAYS
# widen to 60 without stale prices dragging the reference down — old comps are
# still counted, just quietly.
COMPS_HALFLIFE_DAYS = _f("COMPS_HALFLIFE_DAYS", 14.0)

# A reserved listing is a real signal (someone agreed to buy at that number)
# but reservations fall through, so a confirmed sale outranks it.
RESERVED_WEIGHT = _f("RESERVED_WEIGHT", 0.7)
SOLD_WEIGHT = _f("SOLD_WEIGHT", 1.0)

# Shrinkage toward a sibling-SKU prior, so a model with 6 comps isn't trusted
# as hard as one with 60 and there's no cliff at MIN_COMPS:
#   ref = (n*observed + PRIOR_WEIGHT*prior) / (n + PRIOR_WEIGHT)
PRIOR_WEIGHT = _f("PRIOR_WEIGHT", 5.0)

# Which quantile of the comps distribution to call "the" resale price. 0.5 is
# the median — what a typical card goes for. Lowering it (0.35-0.40) prices in
# your need to sell reasonably quickly rather than eventually.
REF_PERCENTILE = _f("REF_PERCENTILE", 0.5)

# A reserved listing must vanish from this many consecutive comps runs
# before we treat it as sold.
MISSING_RUNS_FOR_SALE = _i("MISSING_RUNS_FOR_SALE", 2)

# Ignore listings not seen in this long when doing sale inference.
STALE_LISTING_DAYS = _i("STALE_LISTING_DAYS", 21)

# --------------------------------------------------------------- runtime
ALERT_MAX_PAGES = _i("ALERT_MAX_PAGES", 2)
COMPS_MAX_PAGES = _i("COMPS_MAX_PAGES", 5)
REQUEST_DELAY = _f("REQUEST_DELAY", 1.0)      # polite pause between API pages
TELEGRAM_RATE_DELAY = _f("TELEGRAM_RATE_DELAY", 0.5)
HTTP_TIMEOUT = _f("HTTP_TIMEOUT", 20.0)
HTTP_RETRIES = _i("HTTP_RETRIES", 3)
DRY_RUN = os.getenv("DRY_RUN", "0") == "1"

# Sanity band — anything outside this is a typo, a bundle, or a scam listing
# and must never enter the comps pool or trigger an alert. A real GPU under 50
# EUR is essentially always broken, fake, or a bait listing, never a genuine
# flip opportunity — so 50 is a floor, not just a typo guard.
MIN_SANE_PRICE = _f("MIN_SANE_PRICE", 50.0)
MAX_SANE_PRICE = _f("MAX_SANE_PRICE", 1000.0)

# Floor on a price allowed into the reference-price pool.
#
# The reference price answers "what does this card actually sell for", so it is
# built only from prices someone committed to — a reserved listing (a buyer
# agreed) or an inferred sale (reserved, then gone). An *active* asking price
# never enters it: sellers can ask whatever they like, and a pool of asks
# measures optimism, not the market.
#
# The requirement that motivated this floor was "exclude anything under 5 EUR,
# those are typos". This is deliberately set far above that: a GPU listed under
# 50 EUR is not a typo, it is a dead card, a replica, a bare cooler or bait, and
# letting a real 40 EUR listing into the pool drags the median down just as hard
# as a 4 EUR typo would. 50 subsumes the 5 EUR guard rather than replacing it —
# set MIN_COMP_PRICE=5 to get the literal behaviour.
MIN_COMP_PRICE = _f("MIN_COMP_PRICE", 50.0)

# Per-family override of the pool floor, for families where the generic 50 EUR
# means something quite different.
#
# A console search returns the console's *games* in bulk, and a handful of them
# lead with the platform exactly the way a console listing does — "PS5 Spiderman
# 2", "PS5 One Piece Odyssey" are both real, both sampled live on 2026-08-26.
# No wording rule separates those from "PS5 Sony como nueva"; the thing that
# actually separates them is that nobody sells a PlayStation 5 for 38 EUR.
#
# So the floor does the work the words cannot, which is the same argument
# MIN_COMP_PRICE itself rests on. 150 sits far below any real console (a beaten
# Series X is ~300) and far above any game, including a sealed new release.
MIN_COMP_PRICE_BY_FAMILY = {
    "console": _f("MIN_COMP_PRICE_CONSOLE", 150.0),
}

# Ceiling on a price allowed into the reference-price pool, and on a learned
# reference itself. Deliberately NOT MAX_SANE_PRICE.
#
# These answer different questions, exactly as the floors above already do.
# MAX_SANE_PRICE asks "could this listing be a trade I act on", and the owner
# lowered it to 1000 so obvious typos and bundles stop reaching the feed.
# This one asks "is this a real transaction I can learn a price from", and the
# answer there is yes far above anything worth buying: knowing an RTX 5080
# resells for 900 EUR, or an iPhone 17 Pro Max for 1100, is useful even when
# nothing at that price could ever clear MAX_CAPITAL_PRICE.
#
# Coupling them cost real learning. With the pool capped at 1000, every comp
# above that was dropped and — worse — `recompute_model_price` refused to store
# any reference over 1000 at all, so the models with the most expensive comps
# were frozen on their seed guesses permanently. Owner's call on 2026-08-26:
# let the comps run high, keep the alert ceiling low.
MAX_COMP_PRICE = _f("MAX_COMP_PRICE", 4000.0)

# Floor on a comp expressed as a fraction of its own pool's median, applied
# after the absolute floors above.
#
# The absolute floors are blunt by design — one number per family, chosen so it
# cannot hurt the cheapest real product in that family. That leaves them unable
# to say anything about an expensive one: MIN_COMP_PRICE=50 is a meaningful
# guard for an RX 6600 (median 155) and no guard at all for an RX 9070 XT
# (median 640), where a 150 EUR "sale" is bait, a dead card or a listing whose
# price was never the price. Two confirmed sales in the live data sat exactly
# there — "Tarjeta Gráfica Asus 9070 XT" at 150 against 640, and "PlayStation 5
# Pro" at 350 against 760 — and neither says anything wrong in its text, so no
# junk rule can reach them. What marks them is the distance from their peers.
#
# 0.30 is deliberately loose. A worn card really does sell at half its median
# and that is a comp worth having; this is aimed only at the prices that are
# not the same product. Applied to the pool median rather than to any learned
# reference so it works before a model has learned anything.
RELATIVE_COMP_FLOOR = _f("RELATIVE_COMP_FLOOR", 0.30)

# Per-model override of RELATIVE_COMP_FLOOR, for models where the generic 0.30
# is demonstrably too loose.
#
# `ps5_pro` is the one that forced this, and it is a deliberate hand-tuned
# number rather than a rule, so it needs its evidence written down. Two sales
# titled exactly "PlayStation 5 Pro" — 350 EUR and 400 EUR against a 730 EUR
# median — were confirmed by the owner as not Pros. Their descriptions say
# nothing a base PS5 would not ("Consola PlayStation 5 Pro. - Diseño moderno en
# blanco y negro."), and the word "Pro" is exactly the kind of thing a seller
# adds carelessly to a machine worth half as much.
#
# Three cheaper alternatives were tried against the live pools and all three
# failed, which is why the crude number stands:
#
#   * A text rule. There isn't one. Naming the 2TB capacity looked promising
#     until a 1000 EUR Pro turned out not to mention it either, and a 455 EUR
#     one — a real sale — reads exactly like the two fakes.
#   * "A premium variant cannot sell below its base model's median." False in
#     this market: an RTX 3060 Ti (8GB) genuinely sells below an RTX 3060 12GB,
#     and the rule would have destroyed 21 real 3060 Ti comps. It also fails to
#     fire here anyway — 350 is not below the plain PS5's 350 median.
#   * Raising the floor for the whole console family to 0.55. That cuts a real
#     "Xbox Series X Negra" at 250 EUR (2 controllers, all cables, 54% of its
#     own median), and dropping genuine cheap comps biases a reference price
#     upward, which is the more damaging error of the two.
#
# So the override is scoped to the single model whose cheap tail is known to be
# mislabelled base units. 0.55 puts the cut at ~400 EUR, comfortably below the
# cheapest confirmed-real Pro in the pool (455).
RELATIVE_COMP_FLOOR_BY_MODEL = {
    "ps5_pro": _f("RELATIVE_COMP_FLOOR_PS5_PRO", 0.55),
}

# The relative floor needs a pool big enough for its median to mean something.
# Below this many comps the median is one or two listings and filtering against
# it would just amplify whichever of them happened to be wrong.
RELATIVE_FLOOR_MIN_COMPS = _i("RELATIVE_FLOOR_MIN_COMPS", 5)

# How far below the asking price you could realistically negotiate a seller
# down. A listing qualifies if a haggled offer at this discount would clear the
# margin gate, even if the raw asking price alone would not — you can always
# present the offer and see if the seller bites.
OFFER_DISCOUNT = _f("OFFER_DISCOUNT", 0.20)

# Floor on how far below the reference price a listing may sit, as a fraction
# of ref_price, applied before any margin maths.
#
# RE-ENABLED AT 0.65 BY THE OWNER (2026-08-26), stricter than it has ever been.
#
# History, because this setting has now been moved three times and the reasoning
# matters more than the number. It was 0.35 and it worked as designed: the
# margin engine is structurally blind to fakes, because the further from reality
# a price is the better the deal it computes, so replicas, empty boxes, spare
# parts and bait sort straight to the top of a feed ranked by margin.
#
# It was then disabled entirely, because the guard is symmetric and cannot tell
# a scam from a steal: at 0.35 an RTX 3070 against a ~200 EUR reference was
# rejected below 70.23 EUR, so a genuine drawer-clearing sale at 65 EUR was
# dropped while a replica at 71 EUR passed anyway.
#
# Live experience since then reversed that call. Fakes reaching the feed proved
# more costly than missed steals, so the floor is back and set high: a listing
# must now ask at least 65% of what the model is worth.
#
# What 0.65 actually costs, stated plainly because it is a lot. The window
# between this floor and the buy ceiling is narrow. Against a 620 EUR reference
# the asking price must sit between 403 EUR and roughly 585 EUR; against a
# 200 EUR reference, between 130 EUR and 168 EUR. Anything cheaper than that
# floor is now rejected *without being evaluated*, however good it looks — and
# the deepest discounts are exactly where the largest real profits were. This is
# a deliberate trade of upside for a quieter, safer feed.
#
# The window never closes completely: the floor scales with ref_price and the
# ceiling does too, so there is always a band. Set to 0 to disable, 0.35 for the
# original setting.
MIN_PLAUSIBLE_RATIO = _f("MIN_PLAUSIBLE_RATIO", 0.65)

# Ceiling on what may trigger an alert *without* a reference price behind it.
#
# This is the bootstrap path: no comps, no learned ceiling, nothing but a
# keyword match, so the only available protection is a flat cap.
#
# Raised 350 -> 450 by the owner on 2026-08-26. Note what that costs: the flat
# cap was also doing a second, unstated job on this path — keeping whole PCs and
# gaming laptops out of it without having to identify them, since a machine with
# a card in it is rarely listed that cheap. At 450 that second job is weaker, so
# the explicit `whole_machine` rejection in alert_loop is now carrying more of
# the load here than it used to.
#
# Comps are deliberately NOT capped: the reference price needs the full
# distribution to be meaningful.
MAX_ALERT_PRICE = _f("MAX_ALERT_PRICE", 450.0)

# Ceiling on what may trigger an alert *with* a reference price behind it.
#
# A flat 350 applied to every listing was throwing away the largest trades by
# construction. A 4080 at 420 EUR against a 620 EUR reference has to clear a
# 112 EUR required margin and a ~468 EUR buy ceiling — it is a better trade
# than anything the cap allowed through, and it was never evaluated.
#
# Once a model has a reference price the margin gate is a real test, so the only
# remaining job for a cap is bounding capital at risk on a single purchase.
# That is what this is, and it is why it is much higher: it answers "how much am
# I willing to put into one card", not "is this a good deal".
MAX_CAPITAL_PRICE = _f("MAX_CAPITAL_PRICE", 500.0)

# ------------------------------------------------------------------- ops
# observations used to grow by one row per listing per run — ~100k rows/day at a
# 5-minute cadence, which is what forced a 21-day horizon to stay inside the free
# tier. Writing only *changed* observations cut that by orders of magnitude, so
# the horizon no longer has to fight the comps window.
#
# And it was fighting it: 21 days of retention against a 60-day COMPS_WINDOW_DAYS
# meant the purge deleted 39 days of the window the pricer believes it is reading.
# The 14-day halflife built to make a 60-day window safe was doing nothing,
# because the data it was meant to discount gently had already been deleted.
#
# Retention therefore tracks the comps window by default and is floored at it:
# deleting a row the pricer still reads is never a legitimate saving, so a
# too-small override is raised rather than honoured. Storing *more* than the
# window is allowed (useful for backfills), it just buys nothing today.
OBSERVATION_RETENTION_DAYS = max(
    _i("OBSERVATION_RETENTION_DAYS", COMPS_WINDOW_DAYS), COMPS_WINDOW_DAYS
)

# junk_exclusions is a tuning aid, not a record: the phrase lists get adjusted
# against what the filters are catching now, never against last month. It is
# also the table that actually exhausted the free tier — 2.86M rows in 16 days,
# 97% of the database — so it gets the shortest horizon of anything here.
JUNK_RETENTION_DAYS = _i("JUNK_RETENTION_DAYS", 7)

# Consecutive runs returning zero items before the bot reports itself broken.
# The realistic failure is silent: the API changes shape, parsing yields [],
# every run "succeeds" with nothing to say, and the feed just goes quiet.
DEAD_MAN_RUNS = _i("DEAD_MAN_RUNS", 3)

# Persistent-host mode: seconds between passes when run with --loop. Wallapop's
# own search indexing lags new listings by ~150-200s (measured), so polling
# faster than this buys nothing.
LOOP_INTERVAL_SECONDS = _f("LOOP_INTERVAL_SECONDS", 45.0)


def setup_logging() -> None:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )


def require_secrets(*names: str) -> None:
    missing = [n for n in names if not globals().get(n)]
    if missing:
        raise RuntimeError(f"Missing required environment variables: {', '.join(missing)}")
