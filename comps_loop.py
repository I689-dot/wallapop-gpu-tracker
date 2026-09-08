"""Slow loop (hourly): learn what each GPU is actually worth.

Runs the uncapped, nationwide comps searches, records active + reserved state,
infers sales from reserved listings that disappear, and rewrites each model's
reference price and buy-ceiling.
"""

from __future__ import annotations

import logging
import time
import traceback
from datetime import datetime, timedelta

import config
import junk
import models
import pricing
from alert_loop import _check_dead_man, upsert_listing_batches
from alerts import Telegram
from db import Database, iso, now
from wallapop_client import Item, WallapopClient

log = logging.getLogger("comps")

# Hard ceiling on liveness probes per run.
#
# `infer_sales` used to probe every open listing missing from a run, uncapped
# and with no pause between requests — unlike the search path, which sleeps
# REQUEST_DELAY between pages. Each probe can spend up to 14s inside the retry
# backoff on a 403, so as `listings` grows this was the likeliest way to hit the
# 70-minute workflow timeout, and the fastest way to earn a rate-limit that
# looks exactly like the geolocation failure config.py documents at length.
#
# 50 probes at REQUEST_DELAY=1.0 is ~1 minute of the hourly budget in the good
# case. Anything not probed this run simply falls back to the missing_runs
# counter, which is the pre-existing behaviour and loses nothing beyond an hour
# of latency on the closure.
MAX_LIVENESS_PROBES = 50


class _SoldCompsCache:
    """Run-scoped memo over `db.sold_comps`, transparent for everything else.

    The comps loop asked for the same rows twice per model: once directly (for
    median days-to-sale) and once inside `pricing.collect_comps`, which fetches
    its own. At ~40 models that is ~80 round trips a run spent re-reading an
    answer already in memory, and generic keys made it worse — a split-VRAM
    sibling is queried once as its own target and again when the generic key
    borrows from it.

    Caching for the duration of the repricing loop is safe because every
    closure this run will write has already been written: `infer_sales` runs to
    completion before repricing starts, so nothing can change `sold_comps`'
    answer underneath. It is deliberately *not* held across runs.

    `pricing` may not be edited from here, so this is how one fetch reaches
    both callers through its existing `sold_rows` parameter.

    The key is the model alone, deliberately, and that is the whole mechanism:
    every caller derives `since` as `now() - COMPS_WINDOW_DAYS` from its own
    `now()`, so the two windows differ by the microseconds between the two
    calls. Keying on the timestamp would therefore never hit. A row could in
    principle sit inside that gap — meaning it closed almost exactly 60 days
    ago, at a time-decay weight of ~0.05 — which is not a difference any
    reference price can express. The first caller's window is the one used.
    """

    def __init__(self, db: Database) -> None:
        self._db = db
        self._cache: dict[str, list[dict]] = {}

    def __getattr__(self, name: str) -> object:
        # Only reached for names not found on this instance, so sold_comps
        # below still wins and everything else goes straight to the real
        # Database.
        return getattr(self._db, name)

    def sold_comps(self, model_key: str, since: datetime) -> list[dict]:
        if model_key not in self._cache:
            self._cache[model_key] = self._db.sold_comps(model_key, since)
        return self._cache[model_key]


def infer_sales(
    db: Database,
    covered_models: set[str],
    seen_ids: set[str],
    wp: WallapopClient | None = None,
) -> int:
    """Decide which listings have closed, and at what price.

    Wallapop exposes no sold feed, so absence has always been the proxy. But
    absence from *our search results* is a weak signal — a listing that merely
    slid down the ranking looks identical to one that sold. The detail endpoint
    settles it directly: it 404s once a listing is gone, so `wp` upgrades the
    heuristic into a fact.

    That cuts both ways, and both directions matter:
      * confirmed gone -> close it now, instead of waiting out
        MISSING_RUNS_FOR_SALE more hourly runs (~2h of stale ceilings);
      * confirmed alive -> reset the counter, which stops a listing that simply
        fell out of the rankings from ever being booked as a phantom sale.

    A failed request returns None and falls back to the counter — a network
    blip must never be read as a sale.

    Probing is bounded on both axes, which it was not:

      * only reserved listings are probed, via `db.open_reserved_listings`. That
        query was written for exactly this ("the only ones worth spending a
        detail request on") and had no callers — the optimisation was designed
        and never wired up. A never-reserved listing cannot produce a sold comp
        anyway: `mark_closed(item_id, None)` is all it can ever yield, so a
        request confirming it is gone buys nothing but the hour of latency the
        missing_runs counter already covers.
      * at most MAX_LIVENESS_PROBES per run, with config.REQUEST_DELAY between
        them, matching how polite the search path already is.
    """
    open_listings = db.get_open_listings_for_models(sorted(covered_models))

    # The probe set is exactly the set where a probe can change an outcome: the
    # listings the `was_reserved` test below can answer yes for, minus anything
    # this run's searches already found (presence is proof of life, so a request
    # would be pure waste).
    #
    # It is the union of two sources for the same reason `was_reserved` is:
    # `open_reserved_listings` finds the sticky flag, including a listing that
    # went reserved and has since un-reserved — the case the flag exists for and
    # the one a status check cannot see. `last_status == 'reserved'` covers rows
    # whose `ever_reserved` is still NULL, which is every listing the alert loop
    # wrote before it started setting the column. Taking only the first source
    # would have quietly dropped those from probing until the backlog aged out.
    probe_ids: set[str] = set()
    if wp is not None:
        probe_ids = {
            row["item_id"]
            for row in db.open_reserved_listings(sorted(covered_models))
            if row.get("item_id")
        }
        probe_ids.update(
            row["item_id"]
            for row in open_listings
            if row.get("last_status") == "reserved" and row.get("item_id")
        )
        probe_ids -= seen_ids

    closed = 0
    probes = 0

    for row in open_listings:
        item_id = row["item_id"]
        if item_id in seen_ids:
            if row.get("missing_runs"):
                db.set_missing_runs(item_id, 0)
            continue

        alive: bool | None = None
        if wp is not None and item_id in probe_ids and probes < MAX_LIVENESS_PROBES:
            if probes:
                time.sleep(config.REQUEST_DELAY)
            probes += 1
            try:
                alive = wp.is_alive(item_id)
            except Exception:
                alive = None

        if alive is True:
            if row.get("missing_runs"):
                db.set_missing_runs(item_id, 0)
            continue

        if alive is None:
            misses = int(row.get("missing_runs") or 0) + 1
            if misses < config.MISSING_RUNS_FOR_SALE:
                db.set_missing_runs(item_id, misses)
                continue

        was_reserved = bool(row.get("ever_reserved")) or row.get("last_status") == "reserved"
        if was_reserved:
            price = db.last_reserved_price(item_id) or row.get("last_price")
            price = float(price) if price is not None else None
            # The pool floors are applied here as well as in pricing.collect_comps,
            # because `sold_price` is read by more than the pool: the dashboard's
            # Sales page lists every row carrying one, and it was showing an
            # "RTX 5070 sold for 1 EUR" alongside real trades. A price the pool
            # would refuse is not a sale price we know, so it is stored the way
            # every other unknown is — as NULL, on the branch below that already
            # means "closed, price unknown".
            floor = config.MIN_COMP_PRICE_BY_FAMILY.get(
                models.family_of(row.get("model_key")) or "", config.MIN_COMP_PRICE
            )
            if not pricing.comp_sane(price, floor):
                log.info(
                    "SOLD %s @ %s — price outside the comp band, stored as unknown",
                    item_id, price,
                )
                price = None
            db.mark_closed(item_id, price)
            log.info(
                "SOLD %s @ %s (%s) — %s",
                item_id,
                price,
                "confirmed gone" if alive is False else "absent from runs",
                (row.get("title") or "")[:50],
            )
        else:
            db.mark_closed(item_id, None)  # closed, uncertain — excluded from comps
        closed += 1

    if probe_ids:
        log.info(
            "liveness: %d of %d reserved listings probed (cap %d)",
            probes, len(probe_ids), MAX_LIVENESS_PROBES,
        )
    return closed


def run_once() -> dict:
    """One full comps pass. Returns a small stats dict."""
    config.setup_logging()

    db = Database()
    run_id = db.start_run("comps")
    stats = {"items_seen": 0, "closed": 0, "models_updated": 0, "errors": 0, "observations": 0}
    telegram = Telegram()

    try:
        searches = db.get_searches("comps")
        if not searches:
            log.warning("No active comps searches configured — run seed.py first")
            return stats

        found: dict[str, tuple[Item, models.Match]] = {}
        junk_rows: list[dict] = []

        with WallapopClient() as wp:
            for search in searches:
                items = wp.search(
                    search["keywords"],
                    min_price=search.get("min_price"),
                    max_price=search.get("max_price"),  # null for comps
                    category_ids=search.get("category_ids"),
                    # NOT most_relevance. See config.COMPS_ORDER_BY — relevance
                    # ranking returns a well-formed *empty* result set when the
                    # request geolocates outside the marketplace, which is every
                    # request this loop has ever made from a GitHub runner.
                    order_by=config.COMPS_ORDER_BY,
                    max_pages=config.COMPS_MAX_PAGES,
                    nationwide=True,
                    time_filter=config.COMPS_TIME_FILTER,
                )
                count = 0
                for item in items:
                    count += 1
                    if item.item_id in found:
                        continue
                    verdict = junk.check(item.title, item.description)
                    if verdict.excluded:
                        # A broken card's price would drag every median down.
                        junk_rows.append(
                            {
                                "item_id": item.item_id,
                                "title": item.title[:300],
                                "phrase": verdict.phrase,
                                "category": verdict.category,
                            }
                        )
                        continue
                    found[item.item_id] = (item, models.classify(item.title, item.description))
                log.info("%-20s %3d items", search["label"], count)

        stats["items_seen"] = len(found)
        db.log_junk(junk_rows)

        # Ordering is forced from both sides: the change comparison needs the
        # state as it was before this pass, but observations have a foreign key
        # to listings, so a listing must exist before its observation can be
        # written. Hence snapshot, upsert, then observe.
        #
        # A whole machine never contributes a model_key, so its price can never
        # reach a comps pool. This is the one place form factor genuinely
        # matters: a prebuilt selling for 900 EUR is a real transaction, just
        # not one in the loose card its title names, and the reference price is
        # the number every buy ceiling is derived from.
        #
        # upsert_listing_batches owns the reserved/non-reserved split that keeps
        # `ever_reserved` sticky, and lives in alert_loop.py so both loops share
        # one row shape — see its docstring for the drift that motivated that.
        prior_states = db.listing_states(list(found))
        upsert_listing_batches(db, found.values())

        stats["observations"] = db.insert_changed_observations(
            [
                {
                    "item_id": item.item_id,
                    "model_key": (
                        match.model_key
                        if match.priceable and not item.whole_machine
                        else None
                    ),
                    "price": item.price,
                    "status": item.status,
                    "seen_at": iso(now()),
                }
                for item, match in found.values()
                if item.price is not None
            ],
            prior_states,
        )

        # Only models whose searches actually ran this cycle may be judged
        # absent — otherwise a model with no comps search would have all its
        # listings "sold" on the first run. A generic search (e.g. "rtx 4060
        # ti") classifies some listings into its split-VRAM children
        # (rtx_4060_ti_8g/16g), which have no search row of their own, so those
        # children are covered too — otherwise their listings would never be
        # checked for closure and would never contribute a sold comp.
        covered = {s["model_key"] for s in searches if s.get("model_key")}
        for key in list(covered):
            covered.update(models.GENERIC_FALLBACKS.get(key, ()))
        if covered:
            with WallapopClient() as liveness:
                stats["closed"] = infer_sales(db, covered, set(found), liveness)

        existing = db.get_model_prices()
        targets = covered | {
            m.model_key for _, m in found.values() if m.priceable and m.model_key
        }
        since = now() - timedelta(days=config.COMPS_WINDOW_DAYS)
        # One fetch of the sold rows, served to both callers. See
        # _SoldCompsCache for why this is a wrapper rather than a plain local.
        sold_cache = _SoldCompsCache(db)
        for model_key in sorted(targets):
            try:
                # sold_rows carries closed_at plus posted_at/first_seen, which
                # is what turns "50 EUR of margin" into "50 EUR of margin in
                # 6 days" — the pricing module can't fetch it itself.
                if pricing.recompute_model_price(
                    sold_cache,
                    model_key,
                    existing.get(model_key),
                    sold_rows=sold_cache.sold_comps(model_key, since),
                ):
                    stats["models_updated"] += 1
            except Exception:
                stats["errors"] += 1
                log.exception("recompute failed for %s", model_key)

        # Housekeeping runs on the slow loop so the alert path stays lean.
        db.purge_old_observations()
        db.purge_old_junk()

        log.info(
            "comps done: %d items, %d closed, %d models repriced",
            stats["items_seen"], stats["closed"], stats["models_updated"],
        )
        return stats

    except Exception:
        stats["errors"] += 1
        trace = traceback.format_exc()
        log.error("comps loop crashed:\n%s", trace)
        try:
            telegram.send_error(f"comps_loop crashed\n{trace[-1200:]}")
        except Exception:
            log.exception("could not deliver error ping")
        raise
    finally:
        notes = f"closed={stats['closed']} repriced={stats['models_updated']}"
        db.finish_run(
            run_id,
            items_seen=stats["items_seen"],
            errors=stats["errors"],
            notes=notes,
        )
        # Runs after finish_run so this run counts toward the streak. The comps
        # loop silently returning nothing for a day is the reason this exists.
        try:
            _check_dead_man(db, telegram, "comps", run_id=run_id, notes=notes)
        except Exception:
            log.warning("dead-man check failed", exc_info=True)
        telegram.close()


if __name__ == "__main__":
    result = run_once()
    print(
        f"items={result['items_seen']} closed={result['closed']} "
        f"repriced={result['models_updated']} errors={result['errors']}"
    )
