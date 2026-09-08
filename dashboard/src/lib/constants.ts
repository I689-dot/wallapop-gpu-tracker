/**
 * ==========================================================================
 *  PORT OF THE TRACKER'S FEE MODEL. KEEP IT IN STEP WITH ../../../config.py.
 * ==========================================================================
 *
 * This file is a hand-maintained TypeScript transcription of `config.py`'s
 * `FeeModel` plus the deal gate in `pricing.py`'s `evaluate()`. Two independent
 * implementations of the same maths is the root cause of a whole class of bug
 * here, and it has already bitten: before this rewrite the dashboard's in-person
 * ceiling ignored `MARGIN_RATE` (every ceiling above ~278 EUR was too high), its
 * deal gate compared the raw asking price rather than the haggled offer to the
 * buy ceiling (so the dashboard and the Telegram feed disagreed about what a
 * deal even was), and nothing applied `MIN_PLAUSIBLE_RATIO` at all — which on a
 * list sorted by margin descending means replicas and empty boxes sort first.
 *
 * The real fix is for the tracker to persist or expose these numbers so there
 * is one source of truth. Until then, every one of the following env vars must
 * be set to the same value here as in the tracker's own environment:
 *
 *   SELLER_FEE  BUYER_FEE  BUYER_FIXED  SHIPPING_IN
 *   TARGET_MARGIN  MARGIN_RATE  SEED_MARGIN_MULTIPLIER
 *   OFFER_DISCOUNT  MIN_PLAUSIBLE_RATIO
 *   MIN_SANE_PRICE  MAX_SANE_PRICE  MAX_DEAL_PRICE  MAX_CAPITAL_PRICE
 *   BLOCKED_CONDITIONS  BLOCKED_SELLERS  ALERTING_FAMILIES
 *
 * The defaults below mirror config.py's defaults, with one exception noted at
 * SELLER_FEE. Defaults are a starting point, not a guarantee — check the
 * tracker's live env before trusting any figure this dashboard renders.
 *
 * A second section at the foot of this file ports `alert_loop`'s scope
 * rejections, which are *not* in `pricing.evaluate()` and were missing here
 * entirely — see the header on `alertScopeRejection`.
 */

function num(name: string, fallback: number): number {
  const raw = process.env[name];
  if (raw === undefined || raw === "") return fallback;
  const parsed = Number(raw);
  return Number.isFinite(parsed) ? parsed : fallback;
}

// ------------------------------------------------------------- fee inputs

/** config.py: `SHIPPED.seller_fee`. Wallapop charges the seller nothing, so the
 *  tracker's default is 0 — note this differs from the 0.10 that older docs
 *  quote. */
export const SELLER_FEE = num("SELLER_FEE", 0);
/** config.py: `SHIPPED.buyer_fee` — the buyer protection fee on a shipped sale. */
export const BUYER_FEE = num("BUYER_FEE", 0.075);
/** config.py: `SHIPPED.buyer_fixed`. */
export const BUYER_FIXED = num("BUYER_FIXED", 0.69);
/** config.py: `SHIPPED.shipping_in`. */
export const SHIPPING_IN = num("SHIPPING_IN", 4.5);

// -------------------------------------------------------- margin required

/** config.py: `TARGET_MARGIN`. The flat euro floor on a flip's profit. */
export const TARGET_MARGIN = num("TARGET_MARGIN", 50);

/**
 * config.py: `MARGIN_RATE`. Minimum return as a fraction of the item's value,
 * applied alongside the flat floor — whichever demands more wins.
 *
 * A flat target scales badly: 50 EUR is a 25% return on a 200 EUR card and an
 * 8% one on a 620 EUR card, for the same work and the same capital at risk. The
 * crossover is TARGET_MARGIN / MARGIN_RATE, so at the defaults anything with a
 * reference under ~278 EUR is still governed by the 50 EUR floor exactly as
 * before, and above that the rate binds.
 */
export const MARGIN_RATE = num("MARGIN_RATE", 0.18);

/**
 * config.py: `SEED_MARGIN_MULTIPLIER`. Extra margin demanded while a model is
 * still priced from its hand-written seed rather than from real comps — a seed
 * that is 25% too high makes every ordinary listing look like a bargain.
 */
export const SEED_MARGIN_MULTIPLIER = num("SEED_MARGIN_MULTIPLIER", 1.6);

// ----------------------------------------------------------- gate inputs

/**
 * config.py: `OFFER_DISCOUNT`. How far below asking you could realistically
 * negotiate. `pricing.evaluate` gates on the *haggled* offer, not the asking
 * price, because you can always present the offer and see if the seller bites.
 * The dashboard must gate on the same number or its deal set is strictly
 * narrower than the alert feed.
 */
export const OFFER_DISCOUNT = num("OFFER_DISCOUNT", 0.2);

/**
 * config.py: `MIN_PLAUSIBLE_RATIO`. Floor on how far below the reference a
 * listing may sit, as a fraction of ref_price.
 *
 * RE-ENABLED AT 0.65 (2026-08-26) — must stay in step with config.py, which is
 * also 0.65. This setting has moved three times; see config.py for the full
 * history and the numbers behind each move.
 *
 * It guards the margin engine's one structural blind spot: the more absurd a
 * price, the better the margin it computes, so replicas and empty boxes sort
 * straight to the top of a list ranked by margin — and this list is ranked by
 * margin. It was off for a while, because the guard is symmetric and cannot
 * tell a scam from a genuine steal; live experience reversed that, and it is
 * now set higher than it has ever been.
 *
 * At 0.65 this is a load-bearing filter, not a long tail: it rejects any
 * listing asking less than 65% of the reference before any margin maths runs.
 * Expect the deal list to be markedly shorter, and expect it to be missing some
 * genuine bargains along with the fakes.
 */
export const MIN_PLAUSIBLE_RATIO = num("MIN_PLAUSIBLE_RATIO", 0.65);

/** config.py: `MIN_SANE_PRICE` / `MAX_SANE_PRICE`. `pricing.evaluate` rejects
 *  anything outside this band before doing any margin maths at all. A GPU under
 *  50 EUR is a dead card, a replica or bait, never a flip. */
export const MIN_SANE_PRICE = num("MIN_SANE_PRICE", 50);
export const MAX_SANE_PRICE = num("MAX_SANE_PRICE", 1000);

/**
 * config.py: `MAX_ALERT_PRICE` (named MAX_DEAL_PRICE here for continuity with
 * this dashboard's existing env var). A scope cap on the raw asking price,
 * applied by `alert_loop` before any margin maths, bounding capital at risk on
 * a single purchase.
 *
 * This is the *bootstrap* cap: it applies only to listings with no reference
 * price behind them, which the deal list does not mirror at all (see
 * `evaluateDeal`). It is kept because the env var predates the split and other
 * callers still read it.
 */
export const MAX_DEAL_PRICE = num("MAX_DEAL_PRICE", 450);

/**
 * config.py: `MAX_CAPITAL_PRICE`. The cap for listings that *do* have a
 * reference price — which is every listing this dashboard's deal list contains.
 *
 * `alert_loop` applies the flat MAX_ALERT_PRICE only on the bootstrap path now.
 * Once a model has a reference the margin gate is a real test, so the only
 * remaining job for a cap is bounding capital at risk on one purchase.
 *
 * Lowered 700 -> 500 on 2026-08-26. Note the interaction with
 * MIN_PLAUSIBLE_RATIO: the floor is a fraction of the reference and this is a
 * flat number, so above a reference of MAX_CAPITAL_PRICE / MIN_PLAUSIBLE_RATIO
 * (~769 EUR at these values) the floor rises above the cap and the model can
 * never qualify at any price. The RTX 4090 and 5090 were dropped from the
 * tracker for exactly this reason.
 */
export const MAX_CAPITAL_PRICE = num("MAX_CAPITAL_PRICE", 500);

/** config.py: `MIN_COMPS`. Below this many comps the tracker won't replace a
 *  seed price, so the dashboard calls the model low-confidence. */
export const MIN_COMPS_FOR_CONFIDENCE = num("MIN_COMPS", 5);

// -------------------------------------------------------------- FeeModel

/** One flip's cost structure — the port of config.py's `FeeModel` dataclass. */
export interface FeeModel {
  sellerFee: number;
  buyerFee: number;
  buyerFixed: number;
  shippingIn: number;
  label: string;
}

/** Shipped both ways: the worst case, and the gate the tracker alerts on. */
export const SHIPPED: FeeModel = {
  sellerFee: SELLER_FEE,
  buyerFee: BUYER_FEE,
  buyerFixed: BUYER_FIXED,
  shippingIn: SHIPPING_IN,
  label: "shipped",
};

/** Local pickup: no Wallapop fees, no shipping. */
export const IN_PERSON: FeeModel = {
  sellerFee: 0,
  buyerFee: 0,
  buyerFixed: 0,
  shippingIn: 0,
  label: "in person",
};

/**
 * `FeeModel.required_margin` — what a flip has to clear, in euros.
 *
 * `isSeed` folds in `pricing._seeded_ceiling`: while a model is priced from a
 * guess rather than from comps, the bar is raised by SEED_MARGIN_MULTIPLIER and
 * relaxes on its own once the model reaches MIN_COMPS.
 */
export function requiredMargin(refPrice: number, isSeed = false): number {
  const base = Math.max(TARGET_MARGIN, MARGIN_RATE * refPrice);
  return isSeed ? base * SEED_MARGIN_MULTIPLIER : base;
}

/** `FeeModel.buy_ceiling` — the highest purchase price that still clears the
 *  required margin. */
export function buyCeiling(fees: FeeModel, refPrice: number, isSeed = false): number {
  const gross = refPrice * (1 - fees.sellerFee);
  return (
    (gross - fees.buyerFixed - fees.shippingIn - requiredMargin(refPrice, isSeed)) /
    (1 + fees.buyerFee)
  );
}

/** `FeeModel.net_margin` — expected profit buying at `buyPrice`, reselling at
 *  `refPrice`. */
export function netMargin(fees: FeeModel, buyPrice: number, refPrice: number): number {
  const revenue = refPrice * (1 - fees.sellerFee);
  const cost = buyPrice * (1 + fees.buyerFee) + fees.buyerFixed + fees.shippingIn;
  return revenue - cost;
}

/** The haggled price `pricing.evaluate` actually tests, rounded the way the
 *  tracker rounds it. */
export function offerPrice(price: number): number {
  return Math.round(price * (1 - OFFER_DISCOUNT) * 100) / 100;
}

/** Net profit if bought at `price` and resold shipped at `refPrice`. */
export function netShipped(price: number, refPrice: number): number {
  return netMargin(SHIPPED, price, refPrice);
}

/** Net profit if bought at `price` and resold in-person at `refPrice`. */
export function netInPerson(price: number, refPrice: number): number {
  return netMargin(IN_PERSON, price, refPrice);
}

/** In-person buy ceiling. With every fee zero this reduces to
 *  `refPrice - requiredMargin(refPrice)` — which, and this is the bug that was
 *  here, is *not* `refPrice - TARGET_MARGIN` once MARGIN_RATE binds. */
export function ceilingInPerson(refPrice: number, isSeed = false): number {
  return buyCeiling(IN_PERSON, refPrice, isSeed);
}

// ------------------------------------------------------------ confidence

export type Confidence = "ok" | "low";

/**
 * How many comps a model's price really rests on.
 *
 * Prefers `n_own` — the count of comps the model owns before borrowing from
 * sibling SKUs — over `n_comps`, which includes borrowed ones. "12 real 4060 Ti
 * 16GB comps" and "1 of its own plus 11 borrowed from the 8GB card" are very
 * different claims about how far to trust a ceiling, and only `n_own`
 * distinguishes them. The column is new, so it may be absent from the live
 * database or null on rows written before it existed; both fall back to
 * `n_comps`.
 */
export function ownCompCount(row: {
  n_own?: number | null;
  n_comps?: number | null;
}): number {
  return row.n_own ?? row.n_comps ?? 0;
}

export function confidenceFor(nComps: number | null, isSeed: boolean | null): Confidence {
  if (isSeed || (nComps ?? 0) < MIN_COMPS_FOR_CONFIDENCE) return "low";
  return "ok";
}

/** Confidence from a whole `model_prices` row, using own comps where available. */
export function confidenceForRow(row: {
  n_own?: number | null;
  n_comps?: number | null;
  is_seed?: boolean | null;
}): Confidence {
  return confidenceFor(ownCompCount(row), row.is_seed ?? null);
}

// ------------------------------------------------------------- deal gate

/** The subset of a `model_prices` row the deal gate needs. */
export interface PricedModel {
  ref_price: number | null;
  buy_ceiling: number | null;
  buy_ceiling_in_person: number | null;
  is_seed: boolean | null;
}

export interface DealVerdict {
  qualifies: boolean;
  /** Why, in the same spirit as `pricing.Deal.reason` — useful when a listing
   *  you expected to see isn't in the list. */
  reason: string;
  refPrice: number | null;
  ceilingShipped: number | null;
  ceilingInPerson: number | null;
  /** The haggled price the gate tested. */
  offer: number | null;
}

/**
 * Port of `pricing.evaluate()`, in its order, for a listing with a reference
 * price. The tracker's bootstrap path (no comps, flat cap, "matches your
 * search") is deliberately not mirrored: the dashboard's deal list is defined by
 * clearing a learned ceiling, and a bootstrap alert has no margin behind it to
 * rank or display.
 */
export function evaluateDeal(price: number | null, model: PricedModel | undefined): DealVerdict {
  const miss = (reason: string, refPrice: number | null = null): DealVerdict => ({
    qualifies: false,
    reason,
    refPrice,
    ceilingShipped: null,
    ceilingInPerson: null,
    offer: null,
  });

  if (price === null) return miss("no price");
  if (price < MIN_SANE_PRICE || price > MAX_SANE_PRICE) return miss("price outside sanity band");

  const refPrice = model?.ref_price ?? null;
  if (refPrice === null || refPrice <= 0) return miss("no reference price");

  // The cap is resolved *after* the reference price, because which cap applies
  // depends on whether there is one — `alert_loop` makes the same decision in
  // the same order. Everything reaching this line is priced, so the capital cap
  // is the one that binds; MAX_DEAL_PRICE governs only the bootstrap path this
  // list deliberately does not mirror.
  if (price > MAX_CAPITAL_PRICE) return miss("above the capital cap", refPrice);

  // Disabled by default (ratio 0), so this never fires — kept because the
  // ratio is an env var and the decision is reversible. See MIN_PLAUSIBLE_RATIO.
  if (price < refPrice * MIN_PLAUSIBLE_RATIO) {
    return miss(`implausibly cheap (${price.toFixed(0)} vs ref ${refPrice.toFixed(0)})`, refPrice);
  }

  const isSeed = model?.is_seed === true;

  // On a seed price the ceilings are recomputed with the seed penalty rather
  // than read from the row: the tracker does the same, because the stored
  // ceiling was derived from a guess and has to clear a higher bar until real
  // comps replace it.
  const ceilingShipped = isSeed
    ? buyCeiling(SHIPPED, refPrice, true)
    : (model?.buy_ceiling ?? buyCeiling(SHIPPED, refPrice));
  const ceilingInPersonValue = isSeed
    ? buyCeiling(IN_PERSON, refPrice, true)
    : (model?.buy_ceiling_in_person ?? buyCeiling(IN_PERSON, refPrice));

  // The gate is on the haggled offer, not the asking price.
  const offer = offerPrice(price);
  const qualifies = offer <= ceilingShipped;

  return {
    qualifies,
    reason: qualifies
      ? "offer clears buy ceiling"
      : `offer still above ceiling ${ceilingShipped.toFixed(0)} EUR`,
    refPrice,
    ceilingShipped,
    ceilingInPerson: ceilingInPersonValue,
    offer,
  };
}

// ------------------------------------------------------------ alert scope

/**
 * ==========================================================================
 *  PORT OF alert_loop.py's SCOPE REJECTIONS. Keep in step with its candidate
 *  loop, not just with pricing.evaluate().
 * ==========================================================================
 *
 * `evaluateDeal` above is a faithful port of `pricing.evaluate()` — and that
 * was the whole problem, because `evaluate()` is not the only gate the tracker
 * applies. Before a listing is ever handed to it, `alert_loop` throws out four
 * classes of listing outright, and none of those rejections live in
 * `pricing.py` where this file was looking:
 *
 *   1. families other than 'gpu'        (ALERTING_FAMILIES)
 *   2. whole machines                   (item.whole_machine)
 *   3. blocked sellers                  (config.BLOCKED_SELLERS)
 *   4. the bottom condition tier        (config.BLOCKED_CONDITIONS)
 *
 * All four are recorded on the `listings` row, so the dashboard can apply them
 * exactly rather than approximating. Without them the deal list is strictly
 * wider than the alert feed and disagrees with it about what a deal is, which
 * is the same class of bug as the fee-model drift documented at the top of this
 * file — just arriving through a different door.
 *
 * The comps loop writes `listings` rows for everything it sees, phones and
 * prebuilts included, precisely so their prices can inform the model. Those
 * rows are data for the pricing engine, never candidates for a trade.
 */

/** `alert_loop.ALERTING_FAMILIES`. Everything else in `models.REGISTRY` — the
 *  iPhone 15/16/17 rows — is tracked for its comps and nothing more.
 *
 *  Applied as a denylist rather than an allowlist on purpose: `family` is a
 *  recent column, so a GPU row written before it existed carries null, and
 *  requiring `family = 'gpu'` would hide real deals to enforce a rule aimed at
 *  handsets. The tracker never sees a null here (`models.Match.family` defaults
 *  to 'gpu'), so null means "written before the column" and is a GPU. */
export const ALERTING_FAMILIES = new Set(
  (process.env.ALERTING_FAMILIES ?? "gpu").split(",").map((f) => f.trim()).filter(Boolean),
);

/** `config.BLOCKED_CONDITIONS`. Only the bottom tier is blocked: `fair`
 *  deliberately stays, because a cosmetic flaw on a working card is exactly the
 *  discount a flip is built on. */
export const BLOCKED_CONDITIONS = new Set(
  (process.env.BLOCKED_CONDITIONS ?? "has_given_it_all")
    .split(",")
    .map((c) => c.trim())
    .filter(Boolean),
);

/** `config.BLOCKED_SELLERS`. Empty by default. The same replica or empty-box
 *  listing reappears under a fresh `item_id` every few days, which defeats the
 *  (item_id, price) alert dedup completely; the seller is the only identifier
 *  that survives a relisting. */
export const BLOCKED_SELLERS = new Set(
  (process.env.BLOCKED_SELLERS ?? "").split(",").map((s) => s.trim()).filter(Boolean),
);

/** The subset of a `listings` row the scope rejections need. */
export interface ScopedListing {
  family?: string | null;
  whole_machine?: boolean | null;
  condition?: string | null;
  seller_id?: string | null;
}

/**
 * Would `alert_loop` even consider this listing, before any margin maths?
 *
 * Returns null when the listing is in scope, or the reason it was rejected —
 * in `alert_loop`'s own order, so a listing you expected on the page can be
 * traced to the same rejection the tracker logged.
 *
 * Null-tolerant throughout: each rejection needs positive evidence. A row
 * predating one of these columns must not be dropped on the strength of a
 * column that was never written.
 */
export function alertScopeRejection(listing: ScopedListing): string | null {
  if (listing.whole_machine === true) return "whole machine";
  if (listing.seller_id && BLOCKED_SELLERS.has(listing.seller_id)) return "blocked seller";
  if (listing.family && !ALERTING_FAMILIES.has(listing.family)) {
    return `family ${listing.family} is comps-only`;
  }
  if (listing.condition && BLOCKED_CONDITIONS.has(listing.condition)) {
    return `condition ${listing.condition}`;
  }
  return null;
}

/** Convenience wrapper: true when the tracker would consider this listing. */
export function inAlertScope(listing: ScopedListing): boolean {
  return alertScopeRejection(listing) === null;
}

// ------------------------------------------------------ comps pool gate

/** config.py: `MIN_COMP_PRICE`. The floor on a price admitted to a reference
 *  pool. Far above the owner's literal "under 5 EUR is a typo": a GPU under
 *  50 EUR is a dead card, a replica or bait, and drags a median just as hard. */
export const MIN_COMP_PRICE = num("MIN_COMP_PRICE", 50);

/** config.py: `MIN_COMP_PRICE_BY_FAMILY['console']`. A console search returns
 *  the console's games in bulk and some are titled exactly like a console, so
 *  the floor does the work no wording rule can. */
export const MIN_COMP_PRICE_CONSOLE = num("MIN_COMP_PRICE_CONSOLE", 150);

/** config.py: `MAX_COMP_PRICE`. Deliberately far above MAX_SANE_PRICE — a sale
 *  the bot would never make is still evidence of what a card is worth. */
export const MAX_COMP_PRICE = num("MAX_COMP_PRICE", 4000);

/**
 * Why a confirmed sale is *not* one of the prices its model learned from —
 * the port of `db.sold_comps`'s predicates plus `pricing.comp_sane`.
 *
 * The Sales page lists every row carrying a `sold_price`, which is the right
 * thing to list: they are all real closures. But only some of them reach a
 * comps pool, and until this existed the page gave no way to tell which — a
 * 1 EUR "RTX 5070" and a 60 EUR "PS5" sat in the same list as real trades,
 * looking like evidence the tracker had acted on when the tracker had in fact
 * refused all three. Same failure mode as the deal list reading no scope
 * columns, and the same fix: port the gate rather than re-derive it.
 *
 * Not ported here: `pricing._drop_far_below_median`, which needs the whole pool
 * and so cannot be decided one row at a time.
 */
export function compsPoolRejection(sale: {
  confidence: string | null;
  whole_machine: boolean | null;
  family: string | null;
  sold_price: number | null;
}): string | null {
  // NULL-tolerant, like the query: rows written before the column existed carry
  // NULL there and are not whole machines.
  if (sale.whole_machine === true) return "whole machine";
  // `in (high, medium)` also excludes NULL, and correctly: confidence is only
  // NULL when model_key is, and such a row matches no model filter anyway.
  if (sale.confidence !== "high" && sale.confidence !== "medium") {
    return `${sale.confidence ?? "unclassified"} confidence`;
  }
  const floor = sale.family === "console" ? MIN_COMP_PRICE_CONSOLE : MIN_COMP_PRICE;
  if (sale.sold_price === null) return "no price";
  if (sale.sold_price < floor) return `under ${floor} EUR floor`;
  if (sale.sold_price > MAX_COMP_PRICE) return `over ${MAX_COMP_PRICE} EUR ceiling`;
  return null;
}
