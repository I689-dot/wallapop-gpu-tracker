import "server-only";
import { supabaseAdmin } from "@/lib/supabase/server";
import {
  ALERTING_FAMILIES,
  MAX_CAPITAL_PRICE,
  MAX_SANE_PRICE,
  MIN_SANE_PRICE,
  compsPoolRejection,
  confidenceForRow,
  relativeFloorRejections,
  evaluateDeal,
  inAlertScope,
  netInPerson,
  netShipped,
} from "@/lib/constants";
import type {
  JunkExclusionRow,
  ListingRow,
  ModelPriceRow,
  ObservationRow,
  SearchRow,
  SentAlertRow,
} from "@/lib/types";

const ALERTS_SOFT_CAP = 3000;
const JUNK_SOFT_CAP = 3000;
const LISTINGS_SOFT_CAP = 5000;

/** PostgREST caps how many rows one request may return (1000 by default), so
 *  anything that must be *complete* rather than merely recent has to page. */
const PAGE_SIZE = 1000;

/** Hard bound on a paged scan, so a runaway table can't turn one page render
 *  into hundreds of requests. Hitting it is reported, never hidden — that is the
 *  whole point: the previous code silently dropped rows past its limit. */
const MAX_SCAN_ROWS = 20_000;

function daysAgoIso(days: number): string {
  return new Date(Date.now() - days * 24 * 60 * 60 * 1000).toISOString();
}

interface PagedResult<T> {
  rows: T[];
  /** Exact number of rows matching the filter, per Postgres. */
  total: number;
  /** True when `rows` is a prefix of `total` because MAX_SCAN_ROWS was reached. */
  truncated: boolean;
}

/**
 * Read every row matching a filter, one page at a time.
 *
 * The row count is taken from an exact `count` on the first page and the loop
 * runs until that many rows are in hand, rather than until a short page comes
 * back. That distinction matters: a server-side `max-rows` lower than PAGE_SIZE
 * would make "short page means done" wrong, and wrong in the silent direction.
 */
async function fetchAllPaged<T>(
  page: (
    from: number,
    to: number,
    withCount: boolean,
  ) => PromiseLike<{ data: unknown; error: unknown; count?: number | null }>,
  maxRows = MAX_SCAN_ROWS,
): Promise<PagedResult<T>> {
  const rows: T[] = [];
  let total = 0;
  let offset = 0;
  let first = true;

  for (;;) {
    const limit = Math.min(PAGE_SIZE, maxRows - offset);
    if (limit <= 0) break;

    const result = await page(offset, offset + limit - 1, first);
    if (result.error) throw result.error;
    const batch = (result.data ?? []) as T[];

    if (first) {
      total = result.count ?? batch.length;
      first = false;
    }
    rows.push(...batch);
    offset += batch.length;

    // A zero-length page means the table ran out before `count` said it would
    // (a concurrent delete, say). Stop rather than spin.
    if (batch.length === 0) break;
    if (rows.length >= total) break;
  }

  return { rows, total, truncated: rows.length < total };
}

// --------------------------------------------------------------- overview

export interface LiveDeal extends ListingRow {
  ref_price: number | null;
  buy_ceiling: number | null;
  buy_ceiling_in_person: number | null;
  is_seed: boolean | null;
  /** The haggled price the margin gate actually cleared (see OFFER_DISCOUNT). */
  offer_price: number | null;
  /** Net margin at the *asking* price — the conservative figure, i.e. what you
   *  make if the seller won't budge. `offer_price` is what qualified the deal;
   *  these are what it's worth if you pay full ask. */
  net_shipped: number;
  net_inperson: number;
}

export interface LiveDealsResult {
  deals: LiveDeal[];
  /** Active listings examined after the price prefilter. */
  scanned: number;
  /** Total matching the prefilter, whether or not they were examined. */
  candidates: number;
  /** True when MAX_SCAN_ROWS cut the scan short, so `deals` may be incomplete.
   *  Surfaced in the UI rather than swallowed. */
  truncated: boolean;
}

async function modelPricesByKey(modelKeys: string[]): Promise<Map<string, ModelPriceRow>> {
  const keys = [...new Set(modelKeys.filter(Boolean))];
  if (keys.length === 0) return new Map();
  const { data, error } = await supabaseAdmin()
    .from("model_prices")
    .select("*")
    .in("model_key", keys);
  if (error) throw error;
  return new Map((data as ModelPriceRow[]).map((m) => [m.model_key, m]));
}

/**
 * Every active listing that currently clears the tracker's margin gate.
 *
 * This used to fetch the 500 most recently *seen* active listings and filter
 * them in JavaScript, which meant that past 500 active rows deals vanished from
 * the page for no reason but `last_seen` ordering, with nothing to indicate it.
 *
 * Now everything expressible in PostgREST is pushed into the query — the status,
 * the sanity band and the asking-price cap, all of which a deal must satisfy by
 * construction — and the remainder is read completely, in pages. The ceiling
 * comparison stays in JavaScript deliberately: it needs the per-model reference
 * price, and expressing it in SQL would mean a third copy of the fee model in a
 * place even harder to keep in step than `constants.ts` already is. The join is
 * one query for ~60 `model_prices` rows instead.
 *
 * "Clears the margin gate" is necessary but not sufficient, and that was a real
 * bug here: `listings` holds every row *both* loops write, and the comps loop
 * writes rows the tracker would never trade — iPhones (tracked for their comps
 * since the registry gained them, and barred from alerting by
 * `alert_loop.ALERTING_FAMILIES`), prebuilts and laptops, blocked sellers,
 * bottom-tier condition. All of them clear a margin gate that knows nothing
 * about any of that, so all of them were being listed as live deals. The two
 * cheap, indexed rejections are pushed into the query so they don't burn
 * MAX_SCAN_ROWS; the rest go through `inAlertScope`.
 */
export async function getLiveDeals(): Promise<LiveDealsResult> {
  const db = supabaseAdmin();

  // Every listing this list can contain is one with a reference price behind
  // it, so the capital cap is the one that binds — not the bootstrap cap, which
  // governs only the unpriced path `evaluateDeal` does not mirror. Prefiltering
  // at 350 here would silently re-hide the high-end deals the tracker was just
  // changed to find, and it would do it in SQL where nothing downstream could
  // report the loss.
  const priceCeiling = Math.min(MAX_CAPITAL_PRICE, MAX_SANE_PRICE);

  // Both scope filters are written to tolerate a null, and have to be: `family`
  // is a recent column, and a row that predates it is a GPU, not a handset. A
  // bare `.eq("family", "gpu")` would drop every listing written before the
  // column existed — enforcing a rule aimed at phones by hiding real deals.
  // `whole_machine` is `not null default false` in schema.sql, but only after
  // its migration has been applied, so the same care is taken there.
  const alertingFamilies = [...ALERTING_FAMILIES].join(",");

  const { rows, total, truncated } = await fetchAllPaged<ListingRow>((from, to, withCount) =>
    db
      .from("listings")
      .select("*", withCount ? { count: "exact" } : undefined)
      .eq("last_status", "active")
      .not("model_key", "is", null)
      .gte("last_price", MIN_SANE_PRICE)
      .lte("last_price", priceCeiling)
      .or(`family.is.null,family.in.(${alertingFamilies})`)
      .or("whole_machine.is.null,whole_machine.eq.false")
      .order("last_price", { ascending: true })
      .range(from, to),
  );

  const prices = await modelPricesByKey(rows.map((l) => l.model_key ?? ""));

  const deals: LiveDeal[] = [];
  for (const listing of rows) {
    // The rejections the query couldn't express (blocked seller, blocked
    // condition), plus a belt-and-braces re-check of the two it could.
    if (!inAlertScope(listing)) continue;

    const mp = listing.model_key ? prices.get(listing.model_key) : undefined;
    const verdict = evaluateDeal(listing.last_price, mp);
    if (!verdict.qualifies) continue;

    const refPrice = verdict.refPrice as number;
    const price = listing.last_price as number;
    deals.push({
      ...listing,
      ref_price: refPrice,
      buy_ceiling: verdict.ceilingShipped,
      buy_ceiling_in_person: verdict.ceilingInPerson,
      is_seed: mp?.is_seed ?? null,
      offer_price: verdict.offer,
      net_shipped: netShipped(price, refPrice),
      net_inperson: netInPerson(price, refPrice),
    });
  }

  deals.sort((a, b) => b.net_shipped - a.net_shipped);
  return { deals, scanned: rows.length, candidates: total, truncated };
}

export interface EnrichedAlert extends SentAlertRow {
  listing: ListingRow | null;
  ref_price: number | null;
  prev_price: number | null;
  net_shipped: number | null;
  net_inperson: number | null;
}

export async function getAlertsEnriched(limit = ALERTS_SOFT_CAP): Promise<EnrichedAlert[]> {
  const db = supabaseAdmin();
  const { data: alertRows, error } = await db
    .from("sent_alerts")
    .select("*")
    .order("sent_at", { ascending: false })
    .limit(limit);
  if (error) throw error;
  const alerts = alertRows as SentAlertRow[];

  const itemIds = [...new Set(alerts.map((a) => a.item_id))];
  const listingsMap = new Map<string, ListingRow>();
  for (let i = 0; i < itemIds.length; i += 200) {
    const chunk = itemIds.slice(i, i + 200);
    const { data, error: lErr } = await db.from("listings").select("*").in("item_id", chunk);
    if (lErr) throw lErr;
    for (const l of data as ListingRow[]) listingsMap.set(l.item_id, l);
  }

  const modelKeys = [...listingsMap.values()].map((l) => l.model_key ?? "");
  const prices = await modelPricesByKey(modelKeys);

  // Previous price per item, in chronological order, to strike through on price_drop.
  const byItemChrono = new Map<string, SentAlertRow[]>();
  for (const a of [...alerts].sort((a, b) => a.sent_at.localeCompare(b.sent_at))) {
    const arr = byItemChrono.get(a.item_id) ?? [];
    arr.push(a);
    byItemChrono.set(a.item_id, arr);
  }
  const prevPriceById = new Map<number, number | null>();
  for (const arr of byItemChrono.values()) {
    for (let i = 0; i < arr.length; i++) {
      prevPriceById.set(arr[i].id, i > 0 ? arr[i - 1].price : null);
    }
  }

  return alerts.map((a) => {
    const listing = listingsMap.get(a.item_id) ?? null;
    const mp = listing?.model_key ? prices.get(listing.model_key) : undefined;
    const refPrice = mp?.ref_price ?? null;
    return {
      ...a,
      listing,
      ref_price: refPrice,
      prev_price: prevPriceById.get(a.id) ?? null,
      net_shipped: refPrice !== null && a.price !== null ? netShipped(a.price, refPrice) : null,
      net_inperson: refPrice !== null && a.price !== null ? netInPerson(a.price, refPrice) : null,
    };
  });
}

export interface OverviewKpis {
  liveDealsCount: number;
  alerts24h: number;
  alerts7d: number;
  listingsActive: number;
  listingsTotal: number;
  modelsOk: number;
  modelsLow: number;
  junk7d: number;
  activeSearches: number;
}

export async function getOverviewKpis(liveDealsCount: number): Promise<OverviewKpis> {
  const db = supabaseAdmin();

  const [alerts24h, alerts7d, listingsActive, listingsTotal, models, junk7d, activeSearches] =
    await Promise.all([
      db
        .from("sent_alerts")
        .select("*", { count: "exact", head: true })
        .gte("sent_at", daysAgoIso(1)),
      db
        .from("sent_alerts")
        .select("*", { count: "exact", head: true })
        .gte("sent_at", daysAgoIso(7)),
      db
        .from("listings")
        .select("*", { count: "exact", head: true })
        .eq("last_status", "active"),
      db.from("listings").select("*", { count: "exact", head: true }),
      // `*` rather than a column list so that `n_own` is picked up once the
      // tracker's migration adds it, without this query failing on a database
      // where it doesn't exist yet.
      db.from("model_prices").select("*"),
      db
        .from("junk_exclusions")
        .select("*", { count: "exact", head: true })
        .gte("seen_at", daysAgoIso(7)),
      db
        .from("searches")
        .select("*", { count: "exact", head: true })
        .eq("active", true),
    ]);

  for (const r of [alerts24h, alerts7d, listingsActive, listingsTotal, junk7d, activeSearches]) {
    if (r.error) throw r.error;
  }
  if (models.error) throw models.error;

  const modelRows = (models.data ?? []) as ModelPriceRow[];
  let modelsOk = 0;
  let modelsLow = 0;
  for (const m of modelRows) {
    if (confidenceForRow(m) === "ok") modelsOk++;
    else modelsLow++;
  }

  return {
    liveDealsCount,
    alerts24h: alerts24h.count ?? 0,
    alerts7d: alerts7d.count ?? 0,
    listingsActive: listingsActive.count ?? 0,
    listingsTotal: listingsTotal.count ?? 0,
    modelsOk,
    modelsLow,
    junk7d: junk7d.count ?? 0,
    activeSearches: activeSearches.count ?? 0,
  };
}

// --------------------------------------------------------------------- models

export async function getModels(): Promise<ModelPriceRow[]> {
  const { data, error } = await supabaseAdmin()
    .from("model_prices")
    .select("*")
    .order("model_key", { ascending: true });
  if (error) throw error;
  return data as ModelPriceRow[];
}

export async function getModel(modelKey: string): Promise<ModelPriceRow | null> {
  const { data, error } = await supabaseAdmin()
    .from("model_prices")
    .select("*")
    .eq("model_key", modelKey)
    .maybeSingle();
  if (error) throw error;
  return data as ModelPriceRow | null;
}

export async function getModelObservations(
  modelKey: string,
  days = 90,
): Promise<ObservationRow[]> {
  const { data, error } = await supabaseAdmin()
    .from("observations")
    .select("*")
    .eq("model_key", modelKey)
    .gte("seen_at", daysAgoIso(days))
    .order("seen_at", { ascending: true })
    .limit(5000);
  if (error) throw error;
  return data as ObservationRow[];
}

// ---------------------------------------------------------------------- sales

export interface SaleRow extends ListingRow {
  /** Non-null by construction — the query requires it. */
  sold_price: number;
  closed_at: string;
  /** How long the listing was actually on sale, in days.
   *
   *  Measured from `posted_at` — the seller's own listing date — falling back to
   *  `first_seen` when it is absent (present on ~95% of sold rows). The fallback
   *  matters: `first_seen` is when the *tracker* noticed, which for anything
   *  listed before the bot existed is far later than when it went on sale, and
   *  would understate the time to sell. */
  days_to_sale: number | null;
  /** Why this sale is not one of the prices its model learned from, or null
   *  when it is. See `compsPoolRejection` — the port of db.sold_comps. */
  pool_rejection: string | null;
}

export interface SalesResult {
  sales: SaleRow[];
  /** Listings that closed without ever being seen reserved, so no price could
   *  be attributed. Reported rather than hidden: it is far and away the larger
   *  number, and it is the reason comps accumulate slowly. */
  unpricedClosures: number;
  /** Of `sales`, how many the comps pool refuses. Reported for the same reason
   *  as `unpricedClosures`: a confirmed closure the tracker learned nothing
   *  from is not the same event as one it did, and the page said so nowhere. */
  outsidePool: number;
  truncated: boolean;
}

/**
 * Every sale the tracker has actually confirmed a price for, newest first.
 *
 * "Sold" is an inference, not a fact Wallapop publishes, and the schema
 * distinguishes two very different outcomes that both set `closed_at`:
 *
 *   - the listing was seen **reserved** and then disappeared. `sold_price` is
 *     the price it carried while reserved — someone committed to that number.
 *     These are the rows below, and they are the only thing the reference
 *     prices are ever built from.
 *   - the listing simply vanished. Might have sold, might have been deleted in
 *     frustration. `comps_loop` writes `sold_price = null` deliberately rather
 *     than guessing, and `collect_comps` never sees it.
 *
 * Filtering on `sold_price` rather than on `closed_at` is therefore the whole
 * point of this page: it is the difference between "what did things sell for"
 * and "what stopped being listed".
 */
export async function getSales(): Promise<SalesResult> {
  const db = supabaseAdmin();

  const [{ rows, truncated }, unpriced] = await Promise.all([
    fetchAllPaged<ListingRow>((from, to, withCount) =>
      db
        .from("listings")
        .select("*", withCount ? { count: "exact" } : undefined)
        .not("closed_at", "is", null)
        .not("sold_price", "is", null)
        .order("closed_at", { ascending: false })
        .range(from, to),
    ),
    db
      .from("listings")
      .select("*", { count: "exact", head: true })
      .not("closed_at", "is", null)
      .is("sold_price", null),
  ]);
  if (unpriced.error) throw unpriced.error;

  const sales = rows.map((r) => {
    const closed = r.closed_at as string;
    const listed = r.posted_at ?? r.first_seen;
    const first = listed ? Date.parse(listed) : NaN;
    const end = Date.parse(closed);
    const days =
      Number.isFinite(first) && Number.isFinite(end)
        ? Math.max(0, Math.round((end - first) / 86_400_000))
        : null;
    return {
      ...r,
      sold_price: r.sold_price as number,
      closed_at: closed,
      days_to_sale: days,
      pool_rejection: compsPoolRejection({
        confidence: r.confidence,
        whole_machine: r.whole_machine,
        family: r.family,
        sold_price: r.sold_price,
      }),
    };
  });

  // The pool's median does not exist until the pool does, so this rejection is
  // the one compsPoolRejection cannot decide per-row. Filled in here.
  const farBelow = relativeFloorRejections(sales);
  for (const s of sales) {
    const reason = farBelow.get(s.item_id);
    if (reason) s.pool_rejection = reason;
  }

  return {
    sales,
    unpricedClosures: unpriced.count ?? 0,
    outsidePool: sales.filter((s) => s.pool_rejection !== null).length,
    truncated,
  };
}

// ------------------------------------------------------------------- listings

export async function getListings(): Promise<ListingRow[]> {
  const { data, error } = await supabaseAdmin()
    .from("listings")
    .select("*")
    .order("last_seen", { ascending: false })
    .limit(LISTINGS_SOFT_CAP);
  if (error) throw error;
  return data as ListingRow[];
}

export async function getListing(itemId: string): Promise<ListingRow | null> {
  const { data, error } = await supabaseAdmin()
    .from("listings")
    .select("*")
    .eq("item_id", itemId)
    .maybeSingle();
  if (error) throw error;
  return data as ListingRow | null;
}

export async function getListingObservations(itemId: string): Promise<ObservationRow[]> {
  const { data, error } = await supabaseAdmin()
    .from("observations")
    .select("*")
    .eq("item_id", itemId)
    .order("seen_at", { ascending: true });
  if (error) throw error;
  return data as ObservationRow[];
}

// ------------------------------------------------------------------- searches

export interface SearchWithCounts extends SearchRow {
  listingsCount: number | null; // null = not derivable (no model_key on this search)
  alertsCount: number | null;
}

interface ModelKeyCounts {
  listings: Map<string, number>;
  alerts: Map<string, number>;
}

/**
 * Per-model listing and alert totals, via the `dashboard_model_key_counts` view.
 *
 * See dashboard/supabase-views.sql — the view has to be applied by hand in the
 * Supabase SQL editor, the same way the tracker's own schema.sql is. Returns
 * null when the view isn't there yet so the caller can fall back rather than
 * break the page.
 */
async function modelKeyCountsFromView(): Promise<ModelKeyCounts | null> {
  const { data, error } = await supabaseAdmin()
    .from("dashboard_model_key_counts")
    .select("model_key, listings_count, alerts_count");

  if (error) {
    // 42P01 = undefined_table; PGRST205 = not found in PostgREST's schema cache.
    // Either means "the view hasn't been created yet", which is expected on a
    // database where the SQL file hasn't been applied. Anything else is real.
    const code = (error as { code?: string }).code;
    if (code === "42P01" || code === "PGRST205") return null;
    throw error;
  }

  const rows = (data ?? []) as {
    model_key: string;
    listings_count: number | null;
    alerts_count: number | null;
  }[];
  return {
    listings: new Map(rows.map((r) => [r.model_key, r.listings_count ?? 0])),
    alerts: new Map(rows.map((r) => [r.model_key, r.alerts_count ?? 0])),
  };
}

/**
 * Same counts, computed client-side by reading both tables in full.
 *
 * Two paged scans rather than the ~130 requests the fan-out version issued, and
 * — more importantly — exact. The old code fetched every `item_id` for a model
 * in a single un-paged request, so for any model with more listings than
 * PostgREST's row cap the id list was quietly cut short and its alert count came
 * back too low, with nothing to indicate it. Counting from a complete scan is
 * the only version of this that is right.
 */
async function modelKeyCountsByScan(): Promise<ModelKeyCounts> {
  const db = supabaseAdmin();

  const listings = await fetchAllPaged<{ item_id: string; model_key: string | null }>(
    (from, to, withCount) =>
      db
        .from("listings")
        .select("item_id, model_key", withCount ? { count: "exact" } : undefined)
        .not("model_key", "is", null)
        .order("item_id", { ascending: true })
        .range(from, to),
  );

  const listingCounts = new Map<string, number>();
  const modelByItem = new Map<string, string>();
  for (const row of listings.rows) {
    if (!row.model_key) continue;
    modelByItem.set(row.item_id, row.model_key);
    listingCounts.set(row.model_key, (listingCounts.get(row.model_key) ?? 0) + 1);
  }

  const alerts = await fetchAllPaged<{ item_id: string }>((from, to, withCount) =>
    db
      .from("sent_alerts")
      .select("item_id", withCount ? { count: "exact" } : undefined)
      .order("id", { ascending: true })
      .range(from, to),
  );

  const alertCounts = new Map<string, number>();
  for (const row of alerts.rows) {
    const key = modelByItem.get(row.item_id);
    if (!key) continue;
    alertCounts.set(key, (alertCounts.get(key) ?? 0) + 1);
  }

  return { listings: listingCounts, alerts: alertCounts };
}

export async function getSearchesWithCounts(): Promise<SearchWithCounts[]> {
  const db = supabaseAdmin();
  const { data: searchRows, error } = await db
    .from("searches")
    .select("*")
    .order("role", { ascending: true })
    .order("label", { ascending: true });
  if (error) throw error;
  const searches = searchRows as SearchRow[];

  const counts = (await modelKeyCountsFromView()) ?? (await modelKeyCountsByScan());

  return searches.map((s) => ({
    ...s,
    listingsCount: s.model_key ? (counts.listings.get(s.model_key) ?? 0) : null,
    alertsCount: s.model_key ? (counts.alerts.get(s.model_key) ?? 0) : null,
  }));
}

export async function getJunkExclusions(limit = JUNK_SOFT_CAP): Promise<JunkExclusionRow[]> {
  const { data, error } = await supabaseAdmin()
    .from("junk_exclusions")
    .select("id, item_id, title, seen_at, rule:category, matched:phrase")
    .order("seen_at", { ascending: false })
    .limit(limit);
  if (error) throw error;
  return data as unknown as JunkExclusionRow[];
}
