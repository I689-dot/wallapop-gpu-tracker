"""Fast loop (every 5 min): find qualifying listings and push Telegram alerts.

Entry point is `run_once()` so the same code runs under GitHub Actions cron
today and inside a `while True` loop on a VM later without changes.
"""

from __future__ import annotations

import logging
import statistics
import sys
import time
import traceback
from datetime import datetime, timedelta
from typing import Iterable

import config
import junk
import models
import pricing
from alerts import Telegram
from db import Database, iso, now
from wallapop_client import Item, WallapopClient

log = logging.getLogger("alert")

# Only these product families may ever produce a Telegram alert. Everything else
# in models.REGISTRY is tracked for its comps and nothing more.
#
# 'console' was added on the owner's instruction on 2026-09-08, reversing the
# comps-only rule the PS5 and Xbox tracking shipped under. The four console
# models had all learned real references by then (ps5 350 / 45 comps,
# ps5_digital 400 / 22, ps5_pro 726 / 18, xbox_series_x 482 / 12), so the
# margin gate is a genuine test for them rather than a seed guess, and all four
# have a non-empty alert window under the current caps.
#
# 'phone' deliberately stays out. The iPhone tracking is a price-learning
# experiment the owner asked for without alerts, and nothing here has been said
# to change that.
ALERTING_FAMILIES = frozenset({"gpu", "console"})

# How long main(--loop) waits after a failed pass, and the ceiling that wait
# backs off to. See main() for why a failed pass must not end the process.
LOOP_BACKOFF_CAP_SECONDS = 15 * 60.0


def listing_row(item: Item, match: models.Match) -> dict:
    """The `listings` row for one observed item. Shared by both loops.

    This function existed twice — once per loop — and the two copies drifted,
    which is the argument for it existing once. Only the alert loop's copy
    wrote `country`, so the row shape the database saw depended on which loop
    happened to touch a listing last: a listing first seen by the comps loop
    had a NULL country until the alert loop happened to see it too, and the
    PGRST204 drift-tolerance path in Database.upsert_listings fired (or didn't)
    depending on load order rather than on the schema. Both loops feed the same
    table for the same downstream consumers, so there is no version of this
    where they should disagree about which columns they fill.

    It lives in alert_loop.py because comps_loop already imports from here
    (`_check_dead_man`), so the dependency direction is established and no new
    module is needed for two functions.
    """
    return {
        "item_id": item.item_id,
        "title": item.title[:500],
        "description": (item.description or "")[:2000],
        "model_key": match.model_key,
        "confidence": match.confidence if match.model_key else None,
        "last_seen": iso(now()),
        "last_price": item.price,
        "last_status": item.status,
        "web_url": item.web_url,
        "image_url": item.image_url,
        "shipping": item.shipping,
        "location": str(item.location) if item.location else None,
        "distance_km": item.distance_km,
        "country": item.country,
        "missing_runs": 0,
        # Structured fields the API hands over that were previously re-derived
        # from free text, or ignored entirely.
        "condition": item.condition,
        "brand": item.brand,
        "taxonomy": list(item.taxonomy) or None,
        "whole_machine": item.whole_machine,
        # Family and capacity come from the classification, not the API. family
        # is what lets a mixed registry be queried safely — "is this row a card
        # or a handset" — and storage is the biggest price driver within one
        # iPhone model, captured now so the pools can be split by capacity later
        # without a backfill that no longer has the titles to parse.
        "family": match.family if match.model_key else None,
        "storage": (
            models.extract_storage(item.title)
            if match.family == "phone"
            else None
        ),
        "posted_at": iso(item.posted_at) if item.posted_at else None,
        "user_allows_shipping": item.user_allows_shipping,
        # The seller id is the only identifier stable across relistings: a
        # replica or empty-box listing reappears under a fresh item_id every
        # few days, which the (item_id, price) alert dedup cannot see at all.
        # Persisted from both loops so BLOCKED_SELLERS can be maintained from
        # what the database actually holds rather than from memory.
        "seller_id": getattr(item, "seller_id", None),
        # "Seller just edited this" — a price cut on a listing that never
        # alerted is otherwise completely invisible, because _decide_kind can
        # only compare against our own alert history. It also means a live
        # seller, which is most of whether an offer gets answered at all.
        "modified_at": iso(item.modified_at) if getattr(item, "modified_at", None) else None,
    }


def upsert_listing_batches(
    db: Database, pairs: Iterable[tuple[Item, models.Match]]
) -> None:
    """Write listing rows, splitting reserved from non-reserved. Both loops.

    `ever_reserved` is sticky and must stay that way: PostgREST's upsert only
    updates the columns present in the payload, so *omitting* the column
    preserves whatever is already stored, while sending `False` would wipe the
    single most valuable fact about a listing. Hence two batches — the
    non-reserved one simply does not mention the column.

    The comps loop has done this since the phantom-sale incident; the alert
    loop never did, and that was the more damaging half. `infer_sales` falls
    back to `last_status == 'reserved'`, which covers the common case, but a
    listing that goes reserved, un-reserves and then vanishes closes with
    `sold_price = None` and yields no comp at all. The 5-minute loop is the one
    that catches short-lived listings, and short-lived listings are the ones
    that actually sold — so the fast loop was silently dropping exactly the
    comps worth the most.
    """
    reserved_rows: list[dict] = []
    active_rows: list[dict] = []
    for item, match in pairs:
        row = listing_row(item, match)
        if item.reserved:
            row["ever_reserved"] = True
            reserved_rows.append(row)
        else:
            active_rows.append(row)
    if reserved_rows:
        db.upsert_listings(reserved_rows)
    if active_rows:
        db.upsert_listings(active_rows)


def _relevant(search: dict, match: models.Match) -> bool:
    """Guard against Wallapop's loose keyword matching.

    `order_by=newest` returns anything vaguely related — a search for "rtx 3070"
    comes back with Ryzen CPUs and wifi cards. Under a bootstrap cap those would
    all "qualify", so a model-targeted search only accepts listings that
    actually classify to that model.

    "Actually classify" means priceable — high or medium confidence. A `low`
    match is precisely the case where the number was found but nothing
    corroborates it, and letting one through here reopens, on the bootstrap
    path, the hole that capping description matches closed on the pricing path:
    the margin engine is skipped entirely (model_row stays None for an
    unpriceable match), so the listing falls through to the search's flat cap
    and alerts as a bare "matches your search".

    Measured on the live feed, two such alerts in three days after the
    description fix landed:

        "Placa gráfica Palit RTX 2070 Super"   -> rtx_3060_ti, 185 EUR
             description: "Tiene el rendimiento de RTX3060TI 8GB"
        "Aoostar AG02 eGPU Adaptador en caja"  -> rtx_4060, 125 EUR

    Neither is the card it matched; both name one in passing. A confidently
    classified model with no comps yet still reaches the bootstrap path, which
    is what that path is for — confidence and having-a-reference-price are
    different questions.
    """
    model_key = search.get("model_key")
    if model_key:
        return match.priceable and models.same_family(model_key, match.model_key)
    if search.get("category_ids"):
        # Broad discovery searches: must at least be an identifiable card.
        return match.priceable
    # Bare keyword watches with no category stay plain keyword matches.
    return True


def _enrich(wp: WallapopClient, item: Item) -> Item:
    """Fill in the fields only the detail endpoint returns.

    Worth one extra request *only* for listings that already cleared the
    margin gate — a handful per run, not the ~400 the search returns. The
    search payload carries no condition at all, and condition is the single
    strongest predictor of a dead card: every false alert measured before this
    change was a listing the API itself labelled `has_given_it_all` or `fair`.
    """
    try:
        detail = wp.fetch_detail(item.item_id)
    except Exception:
        log.warning("detail fetch failed for %s — proceeding on search data", item.item_id)
        return item
    if detail is None:
        return item
    for field in ("condition", "brand", "user_allows_shipping"):
        value = getattr(detail, field, None)
        if value is not None:
            setattr(item, field, value)
    if getattr(detail, "taxonomy", ()):
        item.taxonomy = detail.taxonomy
    return item


def _seller_blocked(item: Item) -> bool:
    """Whether this listing's seller may never trigger an alert.

    The signal exists because the alert dedup is keyed on (item_id, price) and
    a serial relister defeats it completely: the same replica card or empty box
    comes back under a fresh item_id every few days, alerts as brand new every
    time, and no amount of price history helps. The seller id is the only thing
    that survives the relisting.

    `seller_id` is read through getattr because it is a recent addition to
    Item; a build without it simply has no seller signal to enforce, which is
    the pre-existing behaviour rather than a crash.
    """
    seller_id = getattr(item, "seller_id", None)
    return bool(seller_id) and str(seller_id) in config.BLOCKED_SELLERS


def _alert_latency(item: Item) -> float | None:
    """Seconds from the seller pressing publish to us deciding to alert.

    The number that actually matters, and the one nothing was measuring.
    Wallapop's own search indexing accounts for ~150-200s of it before this
    process ever gets a chance to see the listing.
    """
    age = getattr(item, "age_seconds", None)
    return round(age, 1) if age is not None else None


def _decide_kind(price: float, already_sent: list[float]) -> str | None:
    """'new', 'price_drop', or None when this listing+price was already sent."""
    if not already_sent:
        return "new"
    if price in already_sent:
        return None
    return "price_drop" if price < min(already_sent) else None


def run_once() -> dict:
    """One full pass over the alert searches. Returns a small stats dict."""
    config.setup_logging()
    config.require_secrets("TELEGRAM_TOKEN", "TELEGRAM_CHAT_ID")

    db = Database()
    run_id = db.start_run("alert")
    stats: dict = {
        "items_seen": 0,
        "alerts_sent": 0,
        "junk": 0,
        "errors": 0,
        "over_cap": 0,
        "blocked_condition": 0,
        "blocked_seller": 0,
        "whole_machine": 0,
        "non_alerting_family": 0,
        "latency_samples": [],
    }
    telegram = Telegram()
    detail_client: WallapopClient | None = None

    try:
        searches = db.get_searches("alert")
        if not searches:
            log.warning("No active alert searches configured — run seed.py first")
            return stats

        model_prices = db.get_model_prices()

        # item_id -> (item, match, originating search)
        found: dict[str, tuple[Item, models.Match, dict]] = {}
        junk_rows: list[dict] = []

        with WallapopClient() as wp:
            for search in searches:
                # Nationwide/international — Wallapop is one shared marketplace
                # across Spain, Portugal, Italy etc., and there's no API-side
                # price cap here either: the search's max_price is only the
                # bootstrap fallback used below when a model has no learned
                # reference price yet. The real filter is the margin gate.
                items = wp.search(
                    search["keywords"],
                    min_price=search.get("min_price"),
                    category_ids=search.get("category_ids"),
                    order_by="newest",
                    max_pages=config.ALERT_MAX_PAGES,
                    nationwide=True,
                    # 2.5x the items for the same request count, and the
                    # server drops the bottom condition tier before it ever
                    # travels. Both verified live 2026-08-02.
                    time_filter=config.ALERT_TIME_FILTER,
                    condition=config.ALLOWED_CONDITIONS,
                )
                count = kept = 0
                for item in items:
                    count += 1
                    if item.item_id in found:
                        continue
                    verdict = junk.check(item.title, item.description)
                    if verdict.excluded:
                        junk_rows.append(
                            {
                                "item_id": item.item_id,
                                "title": item.title[:300],
                                "phrase": verdict.phrase,
                                "category": verdict.category,
                            }
                        )
                        continue
                    match = models.classify(item.title, item.description)
                    if not _relevant(search, match):
                        continue
                    kept += 1
                    found[item.item_id] = (item, match, search)
                log.info("%-14s %3d returned, %d relevant", search["label"], count, kept)

        stats["items_seen"] = len(found)
        stats["junk"] = len(junk_rows)
        db.log_junk(junk_rows)

        if not found:
            log.info("Nothing to evaluate")
            return stats

        # Record everything we saw, reserved included — the alert loop feeds the
        # comps pool too, and its 5-minute cadence catches short-lived listings
        # the hourly comps loop would miss entirely.
        # Ordering here is forced from both sides: the change comparison needs
        # the listing state as it was *before* this pass, but observations have
        # a foreign key to listings, so a listing has to exist before its
        # observation can be written. Hence snapshot, upsert, then observe.
        #
        # A whole machine's price is recorded but never attributed to a model:
        # a prebuilt that sells for 900 EUR is a real transaction, just not a
        # transaction in the loose card its title happens to name. Nulling the
        # model_key keeps it out of every comps pool at the source, which is
        # the only number that decides a ceiling.
        prior_states = db.listing_states(list(found))
        observation_rows = [
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
            for item, match, _ in found.values()
            if item.price is not None
        ]
        upsert_listing_batches(db, ((i, m) for i, m, _ in found.values()))
        stats["observations"] = db.insert_changed_observations(
            observation_rows, prior_states
        )

        # What this pre-filter may decide is now limited to what can be decided
        # *without* a reference price. The price cap moved into the loop below,
        # because which cap applies is not knowable until the model_prices
        # lookup has happened — and applying the low one to everything was
        # throwing away the largest trades by construction. A 4080 at 420 EUR
        # against a 620 EUR reference has to clear a 112 EUR required margin
        # and a ~468 EUR shipped buy ceiling: a better trade than anything the
        # flat 350 cap ever admitted, dropped here without being evaluated.
        #
        # Reserved listings still go: they price the market, but you can't buy
        # one. Neither can an unpriced listing be evaluated at all.
        #
        # Whole machines now get rejected explicitly, and this is the part the
        # raised cap makes load-bearing. The flat 350 was quietly doing two
        # jobs — bounding capital *and* keeping prebuilts and gaming laptops
        # out of the feed without identifying them, since a machine with a card
        # in it is essentially never listed that cheap. MAX_CAPITAL_PRICE at
        # 700 is squarely inside prebuilt territory, so that second job has to
        # be done on purpose now: off the `whole_machine` taxonomy flag, which
        # is already computed on every Item and already persisted on every row.
        # Note this is a *scope* rejection, not a claim the listing is bad — a
        # card inside a PC can be a fine buy, it just isn't a trade this bot
        # prices, because the reference price it would be judged against is the
        # loose card's.
        #
        # A blocked seller is rejected here too, which is the cheapest place it
        # can possibly happen: before the dedup lookup, before the margin
        # maths, and before the detail request.
        candidates: dict[str, tuple[Item, models.Match, dict]] = {}
        for iid, entry in found.items():
            candidate = entry[0]
            if candidate.reserved or candidate.price is None:
                continue
            if candidate.whole_machine:
                stats["whole_machine"] += 1
                continue
            if _seller_blocked(candidate):
                stats["blocked_seller"] += 1
                log.info(
                    "skip %s — blocked seller %s: %s",
                    iid, getattr(candidate, "seller_id", None), candidate.title[:50],
                )
                continue
            candidates[iid] = entry

        log.info(
            "%d candidates to evaluate (%d whole machines and %d blocked-seller "
            "listings dropped)",
            len(candidates), stats["whole_machine"], stats["blocked_seller"],
        )
        already = db.alerted_prices(list(candidates))
        detail_client = WallapopClient()
        # Rows to write back once the detail endpoint has filled them in — see
        # the batched re-upsert after this loop.
        enriched_rows: list[dict] = []

        for item, match, search in candidates.values():
            model_row = (
                model_prices.get(match.model_key)
                if match.priceable and match.model_key
                else None
            )

            # Families outside ALERTING_FAMILIES are tracked for comps only
            # and must never be sent. iPhones are in the registry so the bot can
            # learn what they actually resell for; the owner asked explicitly
            # for the data without the alerts, and nothing about the margin
            # maths knows that. No alert search targets a phone, so this should
            # be unreachable — which is exactly why it is here rather than left
            # implicit in the seeded search rows. A phone reaching this line
            # means a search row was added or edited somewhere else, and the
            # answer to that is still "do not send it".
            if match.family not in ALERTING_FAMILIES:
                stats["non_alerting_family"] += 1
                log.debug(
                    "skip %s — family %r is comps-only: %s",
                    item.item_id, match.family, item.title[:40],
                )
                continue

            # Two caps, and which one applies is exactly the question this loop
            # position exists to answer. With a reference price behind it the
            # margin gate is a real test, so the only remaining job for a cap
            # is bounding capital at risk on one purchase (MAX_CAPITAL_PRICE).
            # Without one there is nothing but a keyword match, so the flat
            # bootstrap ceiling (MAX_ALERT_PRICE) is the only protection there
            # is. The truthiness test matches pricing.evaluate's own
            # `model_row.get("ref_price")` check exactly, so a row that exists
            # with a null/zero reference takes the bootstrap path in both
            # places rather than getting the generous cap and then failing the
            # gate for a different reason.
            has_ref = bool(model_row and model_row.get("ref_price"))
            cap = config.MAX_CAPITAL_PRICE if has_ref else config.MAX_ALERT_PRICE
            if float(item.price) > cap:
                stats["over_cap"] += 1
                log.debug(
                    "skip %s — %.0f EUR over the %.0f EUR %s cap: %s",
                    item.item_id, item.price, cap,
                    "capital" if has_ref else "bootstrap", item.title[:40],
                )
                continue

            kind = _decide_kind(float(item.price), already.get(item.item_id, []))
            if kind is None:
                continue

            deal = pricing.evaluate(
                float(item.price), model_row, search.get("max_price")
            )
            if not deal.qualifies:
                log.debug("skip %s (%s): %s", item.item_id, item.title[:40], deal.reason)
                continue

            item = _enrich(detail_client, item)
            enriched_rows.append(listing_row(item, match))

            # Form factor is re-checked against the detail payload. The search
            # response usually carries a taxonomy, but not always, and the
            # detail endpoint is the authoritative one — so a prebuilt whose
            # search result arrived untaxonomised is caught here instead of
            # walking through the higher cap.
            if item.whole_machine:
                stats["whole_machine"] += 1
                log.info(
                    "skip %s — whole machine (taxonomy %s): %s",
                    item.item_id, list(item.taxonomy), item.title[:50],
                )
                continue

            # Only the bottom tier is blocked. `fair` deliberately stays: a
            # cosmetic flaw on a card that still works is exactly the discount
            # a flip is built on.
            if item.condition in config.BLOCKED_CONDITIONS:
                stats["blocked_condition"] += 1
                log.info(
                    "skip %s — condition %s: %s",
                    item.item_id, item.condition, item.title[:50],
                )
                continue

            previous = min(already.get(item.item_id, [float(item.price)]))
            try:
                sent = telegram.send_alert(
                    item,
                    deal,
                    kind,
                    previous_price=previous if kind == "price_drop" else None,
                    model_display=match.display,
                )
            except Exception:
                stats["errors"] += 1
                log.exception("send failed for %s", item.item_id)
                continue

            if sent:
                stats["alerts_sent"] += 1
                latency = _alert_latency(item)
                if latency is not None:
                    stats["latency_samples"].append(latency)
                log.info(
                    "ALERT %-12s %s %.0f EUR (%s old) — %s",
                    kind,
                    match.model_key or "unclassified",
                    item.price,
                    f"{latency / 60:.1f}min" if latency is not None else "age unknown",
                    item.title[:60],
                )
                # DRY_RUN never touches Telegram, so it must never touch the
                # dedup table either — a dry-run "send" marking an item as
                # already-alerted would silently suppress the real alert the
                # next time this actually runs for real.
                if not config.DRY_RUN:
                    db.record_alert(item.item_id, float(item.price), kind)

        # Everything _enrich paid a request for used to be thrown away. The
        # listings upsert happens ~50 lines above this point, so the condition,
        # brand, seller-shipping flag and detail taxonomy fetched per qualifying
        # listing were used for the alert caption and the block check and then
        # dropped: no condition value ever reached the database, the dashboard
        # had nothing to show, and "which conditions produced good buys" was
        # unanswerable after the fact. This is a handful of rows per run (only
        # listings that already cleared the margin gate get enriched), and it is
        # one batched write rather than one per listing.
        #
        # These rows deliberately carry no `ever_reserved`: every candidate is
        # non-reserved by construction, and the column's stickiness depends on
        # not being mentioned.
        if enriched_rows:
            db.upsert_listings(enriched_rows)

        log.info(
            "%d listings rejected on the price cap (%.0f EUR with a reference "
            "price, %.0f EUR without)",
            stats["over_cap"], config.MAX_CAPITAL_PRICE, config.MAX_ALERT_PRICE,
        )
        return stats

    except Exception:
        stats["errors"] += 1
        trace = traceback.format_exc()
        log.error("alert loop crashed:\n%s", trace)
        try:
            telegram.send_error(f"alert_loop crashed\n{trace[-1200:]}")
        except Exception:
            log.exception("could not deliver error ping")
        raise
    finally:
        samples = stats["latency_samples"]
        notes = (
            f"junk_filtered={stats['junk']} over_cap={stats['over_cap']} "
            f"blocked_condition={stats['blocked_condition']} "
            f"blocked_seller={stats['blocked_seller']} "
            f"whole_machine={stats['whole_machine']} "
            f"comps_only_family={stats['non_alerting_family']}"
        )
        if samples:
            notes += f" median_latency_s={statistics.median(samples):.0f}"
        db.finish_run(
            run_id,
            items_seen=stats["items_seen"],
            alerts_sent=stats["alerts_sent"],
            errors=stats["errors"],
            notes=notes,
        )
        # Runs after finish_run so this run's own row counts toward the streak.
        try:
            _check_dead_man(db, telegram, run_id=run_id, notes=notes)
        except Exception:
            log.warning("dead-man check failed", exc_info=True)
        if detail_client is not None:
            detail_client.close()
        telegram.close()


# Marker written into run_log.notes when the dead-man switch fires, and read
# back on later runs to suppress a repeat. run_log is already the state this
# check reads, so the cooldown needs no new table and no local file — which is
# the only thing that can work here: every pass is a fresh GitHub Actions runner
# with no memory of the last one, so in-process state would suppress nothing.
DEAD_MAN_MARKER = "dead_man_warned"

# How long the switch stays quiet after firing. Once tripped it stays tripped
# until a human fixes it, and it fired on every subsequent run: a weekend
# outage at the 5-minute cadence is ~1000 identical messages, which is precisely
# how an alert channel stops being read — and an unread channel is the same
# outage the dead-man switch exists to prevent. One message every 6 hours still
# surfaces the failure on the day it happens.
DEAD_MAN_COOLDOWN_HOURS = 6.0

# How much run history to read to find a previous warning. The cooldown can only
# be as long as the window it can see: 60 runs is ~5h at the 5-minute Actions
# cadence, so the cooldown degrades gracefully (one extra message rather than
# silence) instead of the read growing without bound — 6 hours of a 45s --loop
# cadence would be 480 rows every single pass.
DEAD_MAN_HISTORY_RUNS = 60


def _parse_run_time(value: object) -> datetime | None:
    """Best-effort parse of a run_log timestamp; Supabase returns ISO strings."""
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _warned_within_cooldown(runs: list[dict], cutoff: datetime) -> bool:
    """Whether a run at or after `cutoff` already carried the dead-man marker."""
    for run in runs:
        if DEAD_MAN_MARKER not in (run.get("notes") or ""):
            continue
        stamp = _parse_run_time(run.get("started_at") or run.get("finished_at"))
        # An unparsable timestamp on a marked row counts as recent. Every row
        # here came out of the newest-first history window, so it is recent by
        # construction; the only question was whether it is inside the cooldown,
        # and for a warning that otherwise repeats forever, staying quiet is the
        # safer answer to "we can't tell".
        if stamp is None or stamp >= cutoff:
            return True
    return False


def _check_dead_man(
    db: Database,
    telegram: Telegram,
    loop_name: str = "alert",
    *,
    run_id: int | None = None,
    notes: str = "",
) -> None:
    """Warn when a loop has gone quiet rather than actually broken.

    The realistic failure here is silent, not loud: a request starts coming
    back well-formed but empty, parsing yields nothing, every run still
    "succeeds" with nothing to report, and the loop simply stops doing its job.

    This is not hypothetical. The comps loop returned 0 items on all 40 of its
    searches, every hour, for over a day — because it was the only loop *not*
    covered by this check. Nothing surfaced it; the alert feed looked normal
    because a separate code path was still feeding the comps pool. Both loops
    are covered now.

    Firing is rate-limited via `run_log.notes`: a warning writes
    DEAD_MAN_MARKER into this run's row, and a later run that finds the marker
    inside DEAD_MAN_COOLDOWN_HOURS logs instead of messaging. The marker write
    is a second `finish_run` update rather than a field on the first one, and
    that ordering is forced: the streak has to include this run's own row,
    which only exists once finish_run has written it. The cost is that
    `finished_at` is rewritten a few milliseconds later on a warning run, which
    nothing reads to that precision.
    """
    runs = db.recent_runs(loop_name, max(config.DEAD_MAN_RUNS, DEAD_MAN_HISTORY_RUNS))
    streak = runs[: config.DEAD_MAN_RUNS]
    if len(streak) < config.DEAD_MAN_RUNS:
        return
    if not all(int(r.get("items_seen") or 0) == 0 for r in streak):
        return

    log.error(
        "dead-man switch: %d consecutive %s runs saw zero items", len(streak), loop_name
    )
    if _warned_within_cooldown(runs, now() - timedelta(hours=DEAD_MAN_COOLDOWN_HOURS)):
        log.info(
            "dead-man warning already sent within %.0fh — staying quiet",
            DEAD_MAN_COOLDOWN_HOURS,
        )
        return

    telegram.send_error(
        f"{config.DEAD_MAN_RUNS} consecutive {loop_name} runs returned zero "
        "listings.\nEither the API response shape changed or the requests are "
        "geolocating outside the marketplace — run smoke_test.py, which sends "
        "the same time_filter the loops do and prints it, and check the "
        f"'No items parsed' warnings for the section type.\nFurther warnings "
        f"suppressed for {DEAD_MAN_COOLDOWN_HOURS:.0f}h."
    )
    if run_id is not None:
        db.finish_run(run_id, notes=f"{notes} {DEAD_MAN_MARKER}".strip())


def main() -> None:
    """One pass, or a self-paced loop on a persistent host.

    `--loop` exists because the poll gap is the only part of the latency
    budget still worth attacking: Wallapop's indexing costs ~150-200s no
    matter what, so a 45s cadence lands near the floor while a 5-minute one
    adds ~2.5 minutes of pure waiting. Under GitHub Actions the default
    single-pass mode is still correct.

    The two modes want opposite things from a failure, which is why the `try`
    is conditional rather than universal:

      * single pass: re-raise. GitHub Actions decides whether the chain is
        healthy from the exit code, so swallowing a failure there would turn a
        broken deploy into a green build that alerts nobody.
      * `--loop`: log, back off, keep going. Persistent-host mode previously
        had no `try` at all around `while True`, so one transient Supabase blip
        — a connection reset, a 500, a statement timeout — ended the process
        permanently and silently, and the halved time-to-alert that is the
        entire point of this mode could not be relied on. run_once() already
        pings Telegram before re-raising, so the failure is never invisible.

    The backoff doubles per consecutive failure up to LOOP_BACKOFF_CAP_SECONDS
    so a hard outage (expired credentials, an API shape change) doesn't become
    a tight crash-loop hammering Wallapop and Telegram at 45-second intervals —
    which is a good way to convert a temporary problem into a rate-limit. A
    single successful pass resets it.
    """
    once = "--loop" not in sys.argv
    failures = 0
    while True:
        try:
            result = run_once()
        except Exception:
            if once:
                raise
            failures += 1
            wait = min(
                config.LOOP_INTERVAL_SECONDS * 2 ** (failures - 1),
                LOOP_BACKOFF_CAP_SECONDS,
            )
            log.error(
                "pass failed (%d consecutive) — retrying in %.0fs",
                failures, wait, exc_info=True,
            )
            time.sleep(wait)
            continue

        failures = 0
        print(
            f"items={result['items_seen']} alerts={result['alerts_sent']} "
            f"junk={result['junk']} over_cap={result['over_cap']} "
            f"errors={result['errors']}"
        )
        if once:
            return
        time.sleep(config.LOOP_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
