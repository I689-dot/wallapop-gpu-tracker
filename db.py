"""Supabase persistence layer.

All state lives here so the loops themselves stay stateless — that's what makes
an ephemeral GitHub Actions runner (or a future Oracle VM) interchangeable.
"""

from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Sequence

from postgrest.exceptions import APIError
from supabase import Client, create_client

import config

log = logging.getLogger("db")

# PostgREST caps a default select at 1000 rows; ask explicitly for more where
# a model could plausibly have a bigger history.
PAGE = 1000

# Model keys per batch in `get_open_listings_for_models`.
#
# Was briefly 20, to survive that query having no index spanning its three
# predicates. `listings_open_by_model_idx` now exists (applied 2026-10-04), and
# with it the trade reverses: batching is network round trips, so fewer and
# larger is faster — all 66 models in one batch measured 0.41s against 3.04s at
# 20. 50 keeps a margin without giving that up.
#
# The floor on this is not performance but the statement timeout. Without the
# index a 50-key batch measured 7.5s against Supabase's ~8s budget, so if the
# index is ever missing — a restore, a rebuilt project — this is the number
# that decides whether the loop is slow or dead. `_execute_retrying` is what
# makes "slow" the answer.
OPEN_LISTINGS_BATCH = 50


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _num_eq(a: Any, b: Any) -> bool:
    """Numeric equality across the str/Decimal/float mix PostgREST returns."""
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) < 0.005
    except (TypeError, ValueError):
        return False


def chunked(seq: Sequence[Any], size: int = 500) -> Iterable[Sequence[Any]]:
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


class Database:
    def __init__(self, client: Client | None = None) -> None:
        if client is not None:
            self.c = client
        else:
            config.require_secrets("SUPABASE_URL", "SUPABASE_KEY")
            self.c = create_client(config.SUPABASE_URL, config.SUPABASE_KEY)

    # ------------------------------------------------------------- searches
    def get_searches(self, role: str) -> list[dict]:
        res = (
            self.c.table("searches")
            .select("*")
            .eq("role", role)
            .eq("active", True)
            .order("id")
            .execute()
        )
        return res.data or []

    # ----------------------------------------------------------- schema drift
    # Columns this process has learned the live table does not have, keyed by
    # table. Code and schema deploy independently here: a push reaches the
    # runner within minutes while schema.sql is applied by hand, so for a while
    # the code writes columns that do not exist yet. Without this, that window
    # is a hard outage — PostgREST rejects the whole batch with PGRST204 and
    # every row in the run is lost, rather than just the new field.
    #
    # Keyed per table rather than one flat set. It was a single class-level set
    # while only `listings` used this, which was harmless and stopped being so
    # the moment a second table routed through it: a column missing on one table
    # says nothing about another, so one PGRST204 from `model_prices` would have
    # gone on stripping a same-named, perfectly real column out of every later
    # `listings` payload for the life of the process — silent data loss, which is
    # strictly worse than the outage this machinery exists to prevent.
    _missing_columns: dict[str, set[str]] = {}

    @staticmethod
    def _unknown_column(exc: Exception) -> str | None:
        """The column name PostgREST is complaining about, if that's the error."""
        message = str(exc)
        if "PGRST204" not in message and "Could not find" not in message:
            return None
        match = re.search(r"'([^']+)' column", message) or re.search(
            r"Could not find the '([^']+)'", message
        )
        return match.group(1) if match else None

    def _upsert_tolerating_drift(
        self,
        table: str,
        rows: list[dict],
        *,
        on_conflict: str,
        ignore_duplicates: bool = False,
    ) -> None:
        """Upsert `rows`, dropping any column the live table does not have yet.

        Shared by every writer whose payload has grown through ALTER
        statements. It started life inside upsert_listings, which is where it was
        needed first — but the row pricing.recompute_model_price writes carries
        six ALTER-added columns of its own (raw_ref, shrunk, median_days_to_sale,
        n_sold, n_reserved, n_own) and had no such protection, so one un-applied
        migration crashed the comps loop on every single model instead of
        degrading to the columns that do exist. Degrading is clearly right here:
        a reference price missing its provenance fields is still a reference
        price, while a crashed comps run leaves every buy ceiling stale.

        The retry loop terminates because each pass either succeeds, learns a
        new missing column, or re-raises on a column it has already dropped.
        """
        if not rows:
            return
        missing = self._missing_columns.setdefault(table, set())
        payload = [{k: v for k, v in row.items() if k not in missing} for row in rows]
        while True:
            try:
                self.c.table(table).upsert(
                    payload,
                    on_conflict=on_conflict,
                    ignore_duplicates=ignore_duplicates,
                ).execute()
                return
            except Exception as exc:
                column = self._unknown_column(exc)
                if column is None or column in missing:
                    raise
                log.warning(
                    "%s has no column %r yet — dropping it and retrying. "
                    "Apply the ALTER statements in schema.sql to persist it.",
                    table,
                    column,
                )
                missing.add(column)
                payload = [
                    {k: v for k, v in row.items() if k != column} for row in payload
                ]

    # ------------------------------------------------------------- listings
    def upsert_listings(self, rows: list[dict]) -> None:
        for batch in chunked(rows):
            self._upsert_tolerating_drift("listings", list(batch), on_conflict="item_id")

    def get_listings(self, item_ids: Sequence[str]) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for batch in chunked(item_ids, 200):
            res = self.c.table("listings").select("*").in_("item_id", list(batch)).execute()
            for row in res.data or []:
                out[row["item_id"]] = row
        return out

    def get_open_listings_for_models(self, model_keys: Sequence[str]) -> list[dict]:
        """Listings for these models that we still believe are on sale.

        Two things here are deliberate, and both were bugs: this query used one
        batch of 50 model keys and a bare `limit(PAGE)`.

        **It has to page.** `limit(PAGE)` is a cap, not a page, so the result
        was silently truncated the moment a batch's open listings passed 1000 —
        and it had: 2653 open listings across 66 models on 2026-10-03, so the
        first batch returned 1000 of them and the rest were invisible. That is
        worse than the crash it sat next to, because nothing reports it. A
        listing missing from this result never has `missing_runs` incremented,
        so it is never inferred closed, so it never becomes a sold comp — the
        pool quietly stops growing and every number downstream still looks
        plausible.

        **The batches have to be small.** `listings` passed 100k rows in early
        October (43k three weeks earlier), and with no index spanning this
        query's three predicates a 50-key batch measured 7.5s against
        Supabase's 8s statement timeout. That is why the comps loop had been
        failing on roughly every other run with `57014 canceling statement due
        to statement timeout`, crashing before `infer_sales` could close
        anything and sending an error ping each time. 20 keys measured ~0.5s.

        `listings_open_by_model_idx` (schema.sql, applied 2026-10-04) is what
        makes it fast rather than merely survivable: 656 kB of index against
        the 71 MB `listings_last_seen_idx` the planner used to fall back on,
        and it discards no rows at all.

        The sort order is `(model_key, last_seen, item_id)`, which is not
        cosmetic: it is the column order of `listings_open_by_model_idx`, so
        one index scan satisfies both the filter and the sort and Postgres
        discards nothing. Measured with the index in place, ordering by
        `last_seen` first instead cost twice as much (54ms vs 25ms) *and*
        scanned 53691 rows it then threw away, because the planner prefers
        `listings_last_seen_idx` to avoid a sort and pays for it in filtering.
        That is the shape that degrades as the table grows — the same query
        cold measured 3.2s — so matching the index matters more than the
        headline figure suggests.

        `item_id` is last as a tiebreaker rather than for speed: `range()`
        needs a *total* order, or rows sharing a `(model_key, last_seen)` can
        shift between pages and be dropped and duplicated across the boundary.
        """
        if not model_keys:
            return []
        cutoff = iso(now() - timedelta(days=config.STALE_LISTING_DAYS))
        out: list[dict] = []
        for batch in chunked(model_keys, OPEN_LISTINGS_BATCH):
            keys = list(batch)
            out.extend(
                self._paged(
                    lambda lo, hi, keys=keys: self.c.table("listings")
                    .select("item_id,model_key,last_price,last_status,ever_reserved,"
                            "missing_runs,last_seen,title")
                    .in_("model_key", keys)
                    .in_("last_status", ["active", "reserved"])
                    .gte("last_seen", cutoff)
                    .order("model_key")
                    .order("last_seen")
                    .order("item_id")
                    .range(lo, hi)
                )
            )
        return out

    def _paged(self, build: Any, page: int = PAGE) -> list[dict]:
        """Read every row a query matches, one page at a time.

        `build(lo, hi)` must return a query already carrying a stable `order()`
        and the `range(lo, hi)` for the window asked for. Stops on the first
        short page, which is the only reliable end-of-data signal PostgREST
        gives without a second counting round trip.
        """
        out: list[dict] = []
        start = 0
        while True:
            rows = self._execute_retrying(build(start, start + page - 1))
            out.extend(rows)
            if len(rows) < page:
                return out
            start += page

    @staticmethod
    def _execute_retrying(query: Any, attempts: int = 3) -> list[dict]:
        """Run a read, retrying a Postgres statement timeout.

        57014 is the server cancelling a statement that ran past the
        configured limit, and on a table this size it is load- and
        cache-dependent rather than deterministic: the same query that times
        out cold completes in a fraction of the budget warm. One crash from one
        slow read used to abort a whole comps pass and send an error ping, so
        the retry is worth far more than the second or two it can cost.

        Deliberately narrow. Only 57014 is retried — every other APIError is a
        real fault (a dropped column, a bad filter) that retrying cannot fix
        and would only delay surfacing.
        """
        delay = 1.0
        for attempt in range(1, attempts + 1):
            try:
                return query.execute().data or []
            except APIError as exc:
                if str(getattr(exc, "code", "") or exc.json().get("code", "")) != "57014":
                    raise
                if attempt == attempts:
                    raise
                log.warning(
                    "statement timeout on attempt %d/%d, retrying in %.0fs",
                    attempt, attempts, delay,
                )
                time.sleep(delay)
                delay *= 2
        return []

    # --------------------------------------------------------- observations
    def insert_observations(self, rows: list[dict]) -> None:
        for batch in chunked(rows):
            self.c.table("observations").insert(list(batch)).execute()

    def listing_states(self, item_ids: Sequence[str]) -> dict[str, tuple]:
        """(last_price, last_status) per listing, as it stands right now.

        Read *before* upsert_listings overwrites it, and passed back into
        insert_changed_observations afterwards. The two-step exists because the
        ordering is forced from both sides: the comparison needs the old state,
        but observations.item_id has a foreign key to listings, so the row has
        to exist before its observation can be written. Writing observations
        first crashed every production run with a 23503 the moment a listing
        appeared that we had never seen before.
        """
        return {
            item_id: (row.get("last_price"), row.get("last_status"))
            for item_id, row in self.get_listings(item_ids).items()
        }

    def insert_changed_observations(
        self, rows: list[dict], previous: dict[str, tuple] | None = None
    ) -> int:
        """Record only the observations that say something new.

        An observation exists to capture a *change* — a price cut, or a listing
        going reserved. Writing one every pass regardless meant a listing that
        sat untouched for three weeks produced ~6000 identical rows, and the
        comps pool then had to dedup them right back down to one price per item
        anyway. It is the same waste that made junk_exclusions 97% of the
        database, just slower.

        Skipping unchanged rows loses nothing downstream: collect_comps takes
        one price per item, and last_reserved_price takes the newest reserved
        row — both of which the change-only trail still answers exactly.
        """
        if not rows:
            return 0
        if previous is None:
            previous = self.listing_states([r["item_id"] for r in rows])
        fresh = []
        for row in rows:
            prior = previous.get(row["item_id"])
            if prior is not None:
                last_price, last_status = prior
                if _num_eq(last_price, row.get("price")) and last_status == row.get("status"):
                    continue
            fresh.append(row)
        self.insert_observations(fresh)
        return len(fresh)

    def reserved_comps(self, model_key: str, since: datetime) -> list[dict]:
        """Reserved observations for a model inside the trailing window."""
        res = (
            self.c.table("observations")
            .select("item_id,price,seen_at")
            .eq("model_key", model_key)
            .eq("status", "reserved")
            .gte("seen_at", iso(since))
            .order("seen_at", desc=True)
            .limit(PAGE)
            .execute()
        )
        return res.data or []

    def sold_comps(self, model_key: str, since: datetime) -> list[dict]:
        """Listings inferred sold (reserved, then gone) inside the window.

        Also the source of the listed-at/closed-at pairs behind
        median_days_to_sale — hence first_seen/posted_at in the select. There
        used to be a second method (`sold_durations`) that returned
        `self.sold_comps(...)` verbatim under a different docstring, and the
        comps loop called both, running this exact query twice per model
        (~80 redundant round trips a run). One query, one caller, rows passed
        through to pricing.time_to_sale_days.

        Two exclusions, and they are the whole reason this is not a bare
        `select where last_status='closed'`:

        `whole_machine` rows are out: a gaming laptop or a prebuilt that sold
        for 900 EUR is a real transaction, but it is not a transaction in the
        loose card its title names, and letting it into the pool moves the
        reference price by a whole tier. The predicate is NULL-tolerant because
        `= false` is not: in Postgres it drops NULLs, and every listing written
        before that column existed — or during a window where
        `_missing_columns` had stripped it from the payload — carries NULL
        there and was permanently invisible to this query. schema.sql now
        backfills and NOT NULLs the column so the invariant is true at the
        storage layer too; this side stays tolerant for the rows already in
        flight.

        Low-confidence rows are out as well, which is the guard this query was
        missing entirely. `reserved_comps` reads observations.model_key, which
        both loops populate only when `match.priceable` (high/medium) — but
        this one reads listings.model_key, which the loops set at *any*
        confidence. A bare-number or wrong-vendor misclassification — exactly
        what models._confidence exists to catch — was therefore barred from the
        reserved pool and then walked straight into the sold pool at
        SOLD_WEIGHT=1.0, the heaviest weight in the model. `in (high, medium)`
        also excludes NULL, which is correct: confidence is only NULL when
        model_key is NULL, and such a row cannot match the model_key filter
        above anyway.
        """
        res = (
            self.c.table("listings")
            .select("item_id,sold_price,closed_at,first_seen,posted_at")
            .eq("model_key", model_key)
            .in_("confidence", ["high", "medium"])
            .eq("last_status", "closed")
            .not_.is_("sold_price", "null")
            .or_("whole_machine.is.null,whole_machine.eq.false")
            .gte("closed_at", iso(since))
            .limit(PAGE)
            .execute()
        )
        return res.data or []

    def open_reserved_listings(self, model_keys: Sequence[str]) -> list[dict]:
        """Reserved listings we still believe are live, for direct liveness checks.

        These are the only ones worth spending a detail request on: a reserved
        listing that disappears is the single event that produces a sold comp.
        """
        if not model_keys:
            return []
        cutoff = iso(now() - timedelta(days=config.STALE_LISTING_DAYS))
        out: list[dict] = []
        for batch in chunked(model_keys, 50):
            res = (
                self.c.table("listings")
                .select("item_id,model_key,last_price,last_status,ever_reserved,missing_runs,title")
                .in_("model_key", list(batch))
                .in_("last_status", ["active", "reserved"])
                .eq("ever_reserved", True)
                .gte("last_seen", cutoff)
                .limit(PAGE)
                .execute()
            )
            out.extend(res.data or [])
        return out

    def last_reserved_price(self, item_id: str) -> float | None:
        """Price the item carried the last time we saw it reserved."""
        res = (
            self.c.table("observations")
            .select("price")
            .eq("item_id", item_id)
            .eq("status", "reserved")
            .order("seen_at", desc=True)
            .limit(1)
            .execute()
        )
        rows = res.data or []
        price = rows[0]["price"] if rows else None
        return float(price) if price is not None else None

    def mark_closed(self, item_id: str, sold_price: float | None) -> None:
        self.c.table("listings").update(
            {
                "last_status": "closed",
                "closed_at": iso(now()),
                "sold_price": sold_price,
            }
        ).eq("item_id", item_id).execute()

    def set_missing_runs(self, item_id: str, value: int) -> None:
        self.c.table("listings").update({"missing_runs": value}).eq(
            "item_id", item_id
        ).execute()

    # ---------------------------------------------------------- model prices
    def get_model_prices(self) -> dict[str, dict]:
        res = self.c.table("model_prices").select("*").limit(PAGE).execute()
        return {row["model_key"]: row for row in (res.data or [])}

    def upsert_model_price(self, row: dict) -> None:
        # Routed through the drift-tolerant writer for the same reason listings
        # is: the row pricing.recompute_model_price builds carries six
        # ALTER-added columns, and this used to be a plain upsert that turned
        # one un-applied migration into "recompute failed for <model>" on every
        # single model, every run.
        self._upsert_tolerating_drift("model_prices", [row], on_conflict="model_key")

    # ----------------------------------------------------------- sent_alerts
    def alerted_prices(self, item_ids: Sequence[str]) -> dict[str, list[float]]:
        """Every price we've already alerted, per item."""
        out: dict[str, list[float]] = {}
        for batch in chunked(item_ids, 200):
            res = (
                self.c.table("sent_alerts")
                .select("item_id,price")
                .in_("item_id", list(batch))
                .limit(PAGE)
                .execute()
            )
            for row in res.data or []:
                out.setdefault(row["item_id"], []).append(float(row["price"]))
        return out

    def record_alert(self, item_id: str, price: float, kind: str) -> None:
        # The (item_id, price) unique constraint is the real dedup guarantee;
        # ignore_duplicates makes a concurrent double-run a no-op rather than a
        # crash.
        self.c.table("sent_alerts").upsert(
            {"item_id": item_id, "price": price, "kind": kind},
            on_conflict="item_id,price",
            ignore_duplicates=True,
        ).execute()

    # ------------------------------------------------------------- auditing
    def log_junk(self, rows: list[dict]) -> None:
        """Record why a listing was excluded, at most once per listing.

        This table exists to tune the filters, so one row per exclusion is all
        it was ever meant to hold. Plain inserts made it one row per exclusion
        *per run*: the same "Funda iPhone 15" was re-logged every five minutes
        for as long as it stayed listed, ~288 times a day. It reached 2.86M
        rows in 16 days — 97% of the database and the reason the free-tier
        quota ran out — while carrying only a few thousand distinct listings.

        Dedup is an upsert against the unique index on item_id, not a read-back
        followed by an insert. The read-back version had a race it lost
        routinely: both loops run overlapping schedules over a shared discovery
        keyword space, so both see the same new junk listing within seconds of
        each other. Whenever the other loop inserted between this loop's
        read-back and its own insert, the insert hit the unique index and raised
        a duplicate-key error — inside the main try of both loops, so the result
        was a crashed run, a Telegram error ping and a re-raise, over a table
        whose only purpose is filter tuning.

        ignore_duplicates makes that collision the no-op it always should have
        been, and removes the read-back round trips (one per 200 ids, every run)
        along with it. Same pattern as record_alert().

        The upsert needs the unique index to exist, and it may not: schema.sql
        declares it, but `create unique index` fails outright on a table that
        already holds duplicate item_ids — which this table did, by millions,
        which is the whole reason the index was added. So an apply that looks
        successful can silently leave the index uncreated, and Postgres then
        answers the upsert with 42P10, "no unique or exclusion constraint
        matching the ON CONFLICT specification".

        That took the alert loop down on the first run after deploy. This is the
        same class of problem `_upsert_tolerating_drift` exists for — code
        reaching a database that has not caught up — so it is handled the same
        way: fall back to the older read-back-and-insert path, once, for the
        life of the process. That path is racy, but it degrades to a duplicate
        row in an audit table rather than a crashed run, and it is exactly what
        this method did before. See the dedup query in schema.sql for how to
        create the index and get the fast path back.
        """
        if not rows:
            return
        deduped = {r["item_id"]: r for r in rows if r.get("item_id")}
        if not deduped:
            return

        if not self._junk_index_missing:
            try:
                for batch in chunked(list(deduped.values())):
                    self._upsert_tolerating_drift(
                        "junk_exclusions",
                        list(batch),
                        on_conflict="item_id",
                        ignore_duplicates=True,
                    )
                return
            except Exception as exc:
                if "42P10" not in str(exc):
                    raise
                self._junk_index_missing = True
                log.warning(
                    "junk_exclusions has no unique index on item_id, so the "
                    "deduplicating upsert is unavailable — falling back to "
                    "read-then-insert for the rest of this process. Run the "
                    "dedup + create-index statements in schema.sql to restore "
                    "it; until then this table can accumulate duplicates."
                )

        self._log_junk_by_readback(deduped)

    # Set once a 42P10 proves the unique index is absent, so the failing upsert
    # is attempted at most once per process rather than on every run.
    _junk_index_missing: bool = False

    def _log_junk_by_readback(self, deduped: dict[str, dict]) -> None:
        """Pre-index fallback: skip ids already recorded, insert the rest.

        Racy by construction — two loops can both read "absent" and both
        insert — but without the unique index there is nothing for that race to
        violate, so the cost is a duplicate audit row rather than an exception.
        """
        known: set[str] = set()
        for batch in chunked(list(deduped), 200):
            try:
                res = (
                    self.c.table("junk_exclusions")
                    .select("item_id")
                    .in_("item_id", list(batch))
                    .limit(PAGE)
                    .execute()
                )
                known.update(r["item_id"] for r in (res.data or []))
            except Exception as exc:
                # Better to risk a duplicate than to lose the audit trail.
                log.warning("junk dedup lookup failed: %s", exc)

        fresh = [row for iid, row in deduped.items() if iid not in known]
        if not fresh:
            return
        for batch in chunked(fresh):
            self.c.table("junk_exclusions").insert(list(batch)).execute()

    def purge_old_junk(self) -> None:
        """Junk rows past the retention horizon.

        Even deduplicated this grows with every new listing the filters reject,
        and nothing reads a months-old exclusion — the phrase lists get tuned
        against what the filters are catching now.
        """
        self._purge_before(
            "junk_exclusions", now() - timedelta(days=config.JUNK_RETENTION_DAYS)
        )

    def _purge_before(self, table: str, cutoff: datetime, *, slice_hours: int = 6) -> None:
        """Delete `table` rows older than `cutoff`, in time slices.

        A single `delete where seen_at < cutoff` is the obvious implementation
        and it does not survive contact with a real backlog: Postgres cancels
        it on the statement timeout (57014) and *nothing* gets deleted, so the
        table only grows and every subsequent run fails the same way. Slicing
        keeps each statement small enough to commit, and bounding the number of
        slices per run stops housekeeping from monopolising a cycle — a
        backlog then drains over several runs instead of never.
        """
        oldest = self._oldest(table)
        if oldest is None:
            return
        window = timedelta(hours=slice_hours)
        start = oldest
        for _ in range(200):  # bounded work per run
            if start >= cutoff:
                return
            end = min(start + window, cutoff)
            try:
                self.c.table(table).delete().lt("seen_at", iso(end)).gte(
                    "seen_at", iso(start)
                ).execute()
            except Exception as exc:
                log.warning("%s purge slice %s failed: %s", table, start, exc)
                return
            start = end

    def _oldest(self, table: str) -> datetime | None:
        try:
            res = (
                self.c.table(table).select("seen_at").order("seen_at").limit(1).execute()
            )
            rows = res.data or []
            if not rows:
                return None
            return datetime.fromisoformat(str(rows[0]["seen_at"]).replace("Z", "+00:00"))
        except Exception as exc:
            log.warning("%s oldest-row lookup failed: %s", table, exc)
            return None

    def purge_old_observations(self) -> None:
        """Keep `observations` inside the free-tier storage budget.

        One row per listing per run at a 5-minute cadence is ~100k rows/day,
        which fills a free Supabase project in weeks. Nothing older than the
        comps window is ever read, so anything past the retention horizon is
        pure cost.
        """
        self._purge_before(
            "observations", now() - timedelta(days=config.OBSERVATION_RETENTION_DAYS)
        )

    def recent_runs(self, loop_name: str, limit: int) -> list[dict]:
        """Last N finished runs, newest first — input to the dead-man switch.

        `notes` is in the select list because the dead-man switch has no state
        of its own: once tripped it fired a Telegram message every single run,
        so a weekend outage was ~1000 identical messages. The cooldown is
        implemented by writing a marker into run_log.notes and looking for it in
        the recent history, which only works if the history actually carries the
        column.
        """
        try:
            res = (
                self.c.table("run_log")
                .select("id,items_seen,alerts_sent,errors,started_at,finished_at,notes")
                .eq("loop_name", loop_name)
                .not_.is_("finished_at", "null")
                .order("started_at", desc=True)
                .limit(limit)
                .execute()
            )
            return res.data or []
        except Exception as exc:
            log.warning("run history read failed: %s", exc)
            return []

    def start_run(self, loop_name: str) -> int | None:
        try:
            res = self.c.table("run_log").insert({"loop_name": loop_name}).execute()
            return (res.data or [{}])[0].get("id")
        except Exception as exc:  # run logging must never break a run
            log.warning("run_log insert failed: %s", exc)
            return None

    def finish_run(self, run_id: int | None, **fields: Any) -> None:
        if run_id is None:
            return
        try:
            self.c.table("run_log").update({"finished_at": iso(now()), **fields}).eq(
                "id", run_id
            ).execute()
        except Exception as exc:
            log.warning("run_log update failed: %s", exc)
