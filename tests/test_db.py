"""The persistence layer, against a fake PostgREST that keeps SQL semantics.

The existing loop tests use a FakeDB that accepts any key and answers every
query with a list — perfect for wiring, useless for the bugs that actually hurt
here, which all live in the *predicates*. `= false` dropping NULL rows, a
duplicate-key insert crashing a run, a low-confidence row walking into the
heaviest-weighted comp pool: none of those are visible unless the fake models
what Postgres really does.

So the fake below is deliberately pedantic about three things:
  * NULL never satisfies eq / in / gte — it only satisfies `is null`;
  * a unique index raises on a duplicate insert, and is a no-op only when the
    writer asked for ignore_duplicates;
  * a payload naming a column the table doesn't have fails the whole batch with
    a PGRST204-shaped error, the way a deployed-but-unmigrated schema does.

Nothing here touches the network.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import Database, iso, now  # noqa: E402


# --------------------------------------------------------------- the fake
class FakeAPIError(Exception):
    """Stands in for postgrest.APIError, which is just a message to db.py."""


# Unique constraints as schema.sql declares them.
UNIQUE_KEYS = {
    "listings": ("item_id",),
    "model_prices": ("model_key",),
    "sent_alerts": ("item_id", "price"),
    "junk_exclusions": ("item_id",),
    "searches": ("label",),
}


def _cmp_key(value):
    """Best-effort ordering key, so ISO timestamps compare as timestamps."""
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
        try:
            return float(value)
        except ValueError:
            return value
    return value


_LITERALS = {"null": None, "true": True, "false": False}


def _literal(raw: str):
    return _LITERALS.get(raw, raw)


def _test_one(row: dict, op: str, column: str, value) -> bool:
    """One PostgREST filter against one row, with Postgres' NULL semantics.

    This is the part that matters: in Postgres a NULL row value satisfies
    neither `= x` nor `in (...)` nor `>= x`. Only `is null` sees it. Getting
    this wrong in the fake would hide exactly the bug the fake exists to catch.
    """
    actual = row.get(column)
    if op == "is":
        return actual is None if value is None or value == "null" else actual == value
    if actual is None:
        return False
    if op == "eq":
        return actual == value
    if op == "in":
        return actual in value
    if op in {"gte", "gt", "lte", "lt"}:
        left, right = _cmp_key(actual), _cmp_key(value)
        if type(left) is not type(right):
            return False
        return {
            "gte": left >= right,
            "gt": left > right,
            "lte": left <= right,
            "lt": left < right,
        }[op]
    raise AssertionError(f"fake does not implement the {op!r} filter")


class FakeQuery:
    """One PostgREST request under construction."""

    def __init__(self, db: "FakeSupabase", table: str, op: str, payload=None, **kw):
        self.db = db
        self.table_name = table
        self.op = op
        self.payload = payload
        self.kw = kw
        self.filters: list[tuple[bool, str, str, object]] = []
        self._negate = False
        self._order: tuple[str, bool] | None = None
        self._limit: int | None = None

    # --- filters
    def _add(self, op: str, column: str, value) -> "FakeQuery":
        self.filters.append((self._negate, op, column, value))
        self._negate = False
        return self

    @property
    def not_(self) -> "FakeQuery":
        self._negate = True
        return self

    def eq(self, column, value):
        return self._add("eq", column, value)

    def in_(self, column, values):
        return self._add("in", column, list(values))

    def gte(self, column, value):
        return self._add("gte", column, value)

    def gt(self, column, value):
        return self._add("gt", column, value)

    def lte(self, column, value):
        return self._add("lte", column, value)

    def lt(self, column, value):
        return self._add("lt", column, value)

    def is_(self, column, value):
        return self._add("is", column, value)

    def or_(self, filters: str):
        """PostgREST's `or=(a.op.v,b.op.v)`, AND-ed with the other filters."""
        parsed = []
        for clause in filters.split(","):
            column, op, raw = clause.split(".", 2)
            parsed.append((op, column, _literal(raw)))
        return self._add("or", "", parsed)

    def order(self, column, desc: bool = False):
        self._order = (column, desc)
        return self

    def limit(self, count):
        self._limit = count
        return self

    # --- execution
    def _matches(self, row: dict) -> bool:
        for negate, op, column, value in self.filters:
            if op == "or":
                ok = any(_test_one(row, o, c, v) for o, c, v in value)
            else:
                ok = _test_one(row, op, column, value)
            if negate:
                ok = not ok
            if not ok:
                return False
        return True

    def execute(self):
        rows = self.db.rows.setdefault(self.table_name, [])
        if self.op == "select":
            hits = [dict(r) for r in rows if self._matches(r)]
            if self._order:
                column, desc = self._order
                hits.sort(key=lambda r: _cmp_key(r.get(column)), reverse=desc)
            if self._limit is not None:
                hits = hits[: self._limit]
            return FakeResult(hits)
        if self.op == "delete":
            keep = [r for r in rows if not self._matches(r)]
            removed = len(rows) - len(keep)
            self.db.rows[self.table_name] = keep
            self.db.deletes.append((self.table_name, removed))
            return FakeResult([])
        if self.op == "update":
            touched = []
            self.db.check_columns(self.table_name, self.payload)
            for row in rows:
                if self._matches(row):
                    row.update(self.payload)
                    touched.append(dict(row))
            return FakeResult(touched)
        return self._write()

    def _write(self):
        payload = self.payload if isinstance(self.payload, list) else [self.payload]
        rows = self.db.rows.setdefault(self.table_name, [])
        key_cols = UNIQUE_KEYS.get(self.table_name)
        written = []
        for row in payload:
            self.db.check_columns(self.table_name, row)
            existing = None
            if key_cols:
                for candidate in rows:
                    if all(candidate.get(k) == row.get(k) for k in key_cols):
                        existing = candidate
                        break
            if existing is not None:
                if self.op == "insert":
                    raise FakeAPIError(
                        "duplicate key value violates unique constraint "
                        f'"{self.table_name}_{"_".join(key_cols)}_uidx" (23505)'
                    )
                if self.kw.get("ignore_duplicates"):
                    continue
                existing.update(row)
                written.append(dict(existing))
                continue
            fresh = dict(row)
            rows.append(fresh)
            written.append(dict(fresh))
        return FakeResult(written)


class FakeResult:
    def __init__(self, data):
        self.data = data


class FakeTable:
    def __init__(self, db: "FakeSupabase", name: str):
        self.db = db
        self.name = name

    def select(self, columns="*", **_kw):
        self.db.selects.append((self.name, columns))
        return FakeQuery(self.db, self.name, "select")

    def insert(self, rows, **kw):
        return FakeQuery(self.db, self.name, "insert", rows, **kw)

    def upsert(self, rows, **kw):
        self.db.upserts.append((self.name, kw))
        return FakeQuery(self.db, self.name, "upsert", rows, **kw)

    def update(self, values, **kw):
        return FakeQuery(self.db, self.name, "update", values, **kw)

    def delete(self, **kw):
        return FakeQuery(self.db, self.name, "delete", **kw)


class FakeSupabase:
    """In-memory stand-in for supabase.Client.

    `columns` is the *live* schema: name a table there and any payload key
    outside its column set fails the batch with a PGRST204-shaped error, which
    is what a runner does when code has deployed and schema.sql hasn't been
    applied yet. Tables left out of it accept anything.
    """

    def __init__(self, rows: dict | None = None, columns: dict | None = None):
        self.rows: dict[str, list[dict]] = {k: [dict(r) for r in v] for k, v in (rows or {}).items()}
        self.columns = columns or {}
        self.selects: list[tuple[str, str]] = []
        self.upserts: list[tuple[str, dict]] = []
        self.deletes: list[tuple[str, int]] = []

    def table(self, name: str) -> FakeTable:
        return FakeTable(self, name)

    def check_columns(self, table: str, row: dict) -> None:
        known = self.columns.get(table)
        if known is None:
            return
        for column in row:
            if column not in known:
                raise FakeAPIError(
                    "{'code': 'PGRST204', 'details': None, 'hint': None, 'message': "
                    f'"Could not find the \'{column}\' column of \'{table}\' '
                    'in the schema cache"}'
                )

    def select_columns_for(self, table: str) -> list[str]:
        return [cols for name, cols in self.selects if name == table]


@pytest.fixture(autouse=True)
def _forget_schema_drift():
    """`_missing_columns` is deliberately class-level (it is knowledge about the
    live schema, not about one connection), so it leaks between tests."""
    Database._missing_columns.clear()
    yield
    Database._missing_columns.clear()


def make_db(**tables) -> tuple[Database, FakeSupabase]:
    fake = FakeSupabase(rows=tables)
    return Database(client=fake), fake


# ------------------------------------------------------------------ B1 / B4
def _closed(item_id, confidence, *, whole_machine=False, price=300.0, model="rtx_3070"):
    """A listing the sale-inference logic has closed at a known price."""
    closed_at = iso(now() - timedelta(days=1))
    return {
        "item_id": item_id,
        "model_key": model,
        "confidence": confidence,
        "last_status": "closed",
        "sold_price": price,
        "closed_at": closed_at,
        "first_seen": iso(now() - timedelta(days=9)),
        "posted_at": iso(now() - timedelta(days=10)),
        "whole_machine": whole_machine,
    }


def test_low_confidence_listing_never_yields_a_sold_comp():
    """B1, and the worst of the findings.

    reserved_comps reads observations.model_key, which the loops only populate
    when match.priceable — so a misclassification is already barred from the
    reserved pool. sold_comps reads listings.model_key, which the loops write at
    *any* confidence, so the same misclassified row used to come back here and
    contribute a sold comp at SOLD_WEIGHT=1.0, the heaviest weight in the model.
    """
    db, _ = make_db(
        listings=[
            _closed("hi", "high"),
            _closed("med", "medium"),
            _closed("lo", "low"),
        ]
    )

    got = db.sold_comps("rtx_3070", now() - timedelta(days=60))

    assert {r["item_id"] for r in got} == {"hi", "med"}


def test_null_confidence_listing_never_yields_a_sold_comp():
    """confidence is NULL exactly when model_key is NULL, so such a row could
    not match the model_key filter anyway — but the predicate must not resurrect
    it either, and `in (...)` correctly excludes NULL."""
    row = _closed("nul", None)
    db, _ = make_db(listings=[row])

    assert db.sold_comps("rtx_3070", now() - timedelta(days=60)) == []


def test_whole_machine_null_rows_are_still_counted():
    """B4. `= false` excludes NULL in Postgres, so every listing written before
    whole_machine existed — or while _missing_columns had stripped it from the
    payload — was permanently invisible to this query."""
    legacy = _closed("legacy", "high")
    legacy["whole_machine"] = None
    db, _ = make_db(listings=[legacy])

    got = db.sold_comps("rtx_3070", now() - timedelta(days=60))

    assert [r["item_id"] for r in got] == ["legacy"]


def test_whole_machine_true_rows_are_still_excluded():
    """The guard the NULL tolerance must not weaken: a prebuilt that sold for
    900 EUR is a real transaction, just not one in the loose card it names."""
    db, _ = make_db(
        listings=[_closed("card", "high"), _closed("pc", "high", whole_machine=True)]
    )

    got = db.sold_comps("rtx_3070", now() - timedelta(days=60))

    assert [r["item_id"] for r in got] == ["card"]


def test_sold_comps_still_filters_model_status_price_and_window():
    other_model = _closed("other", "high", model="rtx_4070")
    still_open = dict(_closed("open", "high"), last_status="active")
    no_price = dict(_closed("noprice", "high"), sold_price=None)
    too_old = dict(_closed("stale", "high"), closed_at=iso(now() - timedelta(days=90)))
    db, _ = make_db(
        listings=[_closed("keep", "high"), other_model, still_open, no_price, too_old]
    )

    got = db.sold_comps("rtx_3070", now() - timedelta(days=60))

    assert [r["item_id"] for r in got] == ["keep"]


def test_sold_comps_returns_the_columns_days_to_sale_needs():
    """pricing.time_to_sale_days needs closed_at plus posted_at/first_seen. It
    gets them from this one query now that sold_durations is gone."""
    db, _ = make_db(listings=[_closed("k", "high")])

    row = db.sold_comps("rtx_3070", now() - timedelta(days=60))[0]

    assert {"item_id", "sold_price", "closed_at", "first_seen", "posted_at"} <= set(row)


def test_sold_durations_is_gone():
    """D3: it returned self.sold_comps(...) verbatim under a docstring promising
    something else, and comps_loop called it *in addition* to the sold_comps call
    inside pricing.collect_comps — the same query twice per model, ~80 extra
    round trips a run. sold_comps(model_key, since) is the single public method.
    """
    assert not hasattr(Database, "sold_durations")


# ----------------------------------------------------------------------- C1
def test_duplicate_junk_row_does_not_raise():
    """C1. Both loops run overlapping schedules over a shared discovery keyword
    space, so both see the same new junk listing within seconds. The old
    read-back-then-insert lost that race routinely, and the duplicate-key error
    landed inside the main try of both loops: crashed run, error ping,
    re-raise — over a table whose only purpose is filter tuning.
    """
    db, fake = make_db(junk_exclusions=[])
    row = {"item_id": "j1", "title": "Funda iPhone 15", "phrase": "funda", "category": "NOT_A_CARD"}

    db.log_junk([row])
    db.log_junk([row])  # the other loop, or the same one a cycle later

    assert len(fake.rows["junk_exclusions"]) == 1
    assert all(kw.get("ignore_duplicates") for _, kw in fake.upserts)


def test_the_fake_really_enforces_the_unique_index():
    """Guards the test above from becoming vacuous: a plain insert of the same
    item_id must still raise, which is what log_junk used to do."""
    db, fake = make_db(junk_exclusions=[{"item_id": "j1"}])

    with pytest.raises(FakeAPIError, match="23505"):
        fake.table("junk_exclusions").insert([{"item_id": "j1"}]).execute()


def test_log_junk_survives_a_table_with_no_unique_index():
    """The regression this fix shipped with, caught in production rather than here.

    schema.sql declares the unique index, but `create unique index` fails on a
    table that already holds duplicate item_ids — which this one did, by
    millions, which is why the index was wanted. So an apply that looked
    successful left the index uncreated, and Postgres answered the upsert with
    42P10. The alert loop crashed on its first run after deploy, every five
    minutes, because log_junk sits inside the main try of both loops.

    Every existing test passed through this, because the fake honours
    on_conflict whether or not an index would really exist. So the situation is
    modelled explicitly: upsert refuses, and log_junk must fall back to the
    read-then-insert path it used before rather than take the run down.
    """
    db, fake = make_db(junk_exclusions=[])

    def _refuse_upsert(name):
        def boom(_rows, **_kw):
            raise FakeAPIError(
                "{'code': '42P10', 'message': 'there is no unique or exclusion "
                "constraint matching the ON CONFLICT specification'}"
            )
        return boom

    class NoUniqueIndex(type(fake)):
        def table(self, name):
            handle = super().table(name)
            if name == "junk_exclusions":
                handle.upsert = _refuse_upsert(name)
            return handle

    fake.__class__ = NoUniqueIndex
    row = {"item_id": "j1", "title": "Funda iPhone 15", "phrase": "funda", "category": "NOT_A_CARD"}

    db.log_junk([row])                       # must not raise
    assert len(fake.rows["junk_exclusions"]) == 1

    db.log_junk([row])                       # already recorded — read-back skips it
    assert len(fake.rows["junk_exclusions"]) == 1

    # And it must stop retrying the doomed upsert for the rest of the process.
    assert db._junk_index_missing is True


def test_a_non_42P10_upsert_failure_still_propagates():
    """The fallback is scoped to the missing-index case. Any other API error is
    a real failure and must not be swallowed into a silent second write path."""
    db, fake = make_db(junk_exclusions=[])

    def _connection_failure(_rows, **_kw):
        raise FakeAPIError("{'code': '08006', 'message': 'connection failure'}")

    class Broken(type(fake)):
        def table(self, name):
            handle = super().table(name)
            if name == "junk_exclusions":
                handle.upsert = _connection_failure
            return handle

    fake.__class__ = Broken

    with pytest.raises(FakeAPIError, match="08006"):
        db.log_junk([{"item_id": "j1", "title": "x", "phrase": "p", "category": "DEFECT"}])
    assert db._junk_index_missing is False


def test_log_junk_collapses_repeats_inside_one_batch():
    """Both loops can see the same listing from two different searches in a
    single pass; sending the id twice in one payload is a 21000 in Postgres."""
    db, fake = make_db(junk_exclusions=[])

    db.log_junk(
        [
            {"item_id": "j2", "phrase": "no funciona", "category": "DEFECT"},
            {"item_id": "j2", "phrase": "para piezas", "category": "DEFECT"},
        ]
    )

    assert len(fake.rows["junk_exclusions"]) == 1


def test_log_junk_ignores_empty_input_and_rows_without_an_item_id():
    db, fake = make_db(junk_exclusions=[])

    db.log_junk([])
    db.log_junk([{"title": "no id", "phrase": "x"}])

    assert fake.rows["junk_exclusions"] == []
    assert fake.upserts == []


def test_log_junk_no_longer_reads_the_table_back():
    """The read-back was a round trip per 200 ids, every run, and the upsert
    makes it redundant as well as racy."""
    db, fake = make_db(junk_exclusions=[])

    db.log_junk([{"item_id": "j3", "phrase": "p", "category": "DEFECT"}])

    assert fake.select_columns_for("junk_exclusions") == []


# ----------------------------------------------------------------------- C4
LIVE_LISTINGS = {"item_id", "title", "last_price", "drift_col"}
LIVE_MODEL_PRICES = {"model_key", "ref_price", "n_comps"}


def test_upsert_listings_drops_a_column_the_live_table_lacks():
    """The deploy window: code reaches the runner in minutes, schema.sql is
    applied by hand. Losing one new field beats losing the whole batch."""
    fake = FakeSupabase(columns={"listings": LIVE_LISTINGS})
    db = Database(client=fake)

    db.upsert_listings([{"item_id": "l1", "title": "t", "country": "IT"}])

    assert fake.rows["listings"] == [{"item_id": "l1", "title": "t"}]
    assert Database._missing_columns["listings"] == {"country"}


def test_upsert_model_price_drops_a_column_the_live_table_lacks():
    """C4: model_prices had none of this protection, while the row
    pricing.recompute_model_price writes carries six ALTER-added columns. One
    un-applied migration meant 'recompute failed' for every model, every run."""
    fake = FakeSupabase(columns={"model_prices": LIVE_MODEL_PRICES})
    db = Database(client=fake)

    db.upsert_model_price(
        {"model_key": "rtx_3070", "ref_price": 300.0, "n_comps": 8, "n_own": 8, "shrunk": True}
    )

    assert fake.rows["model_prices"] == [
        {"model_key": "rtx_3070", "ref_price": 300.0, "n_comps": 8}
    ]
    assert Database._missing_columns["model_prices"] == {"n_own", "shrunk"}
    assert "listings" not in Database._missing_columns


def test_missing_columns_are_tracked_per_table():
    """A column missing on one table says nothing about another. With the single
    shared set this used to be, the first PGRST204 from model_prices would have
    stripped `drift_col` from every later listings payload for the life of the
    process — silent data loss instead of the outage the machinery prevents.
    """
    fake = FakeSupabase(
        columns={"listings": LIVE_LISTINGS, "model_prices": LIVE_MODEL_PRICES}
    )
    db = Database(client=fake)

    db.upsert_model_price({"model_key": "m1", "ref_price": 1.0, "drift_col": 5})
    db.upsert_listings([{"item_id": "l2", "title": "t", "drift_col": 7}])

    assert Database._missing_columns["model_prices"] == {"drift_col"}
    assert fake.rows["listings"][0]["drift_col"] == 7


def test_an_unrelated_error_is_never_swallowed():
    """Only PGRST204 is a schema-drift story. Anything else has to surface."""

    class Exploding(FakeSupabase):
        def check_columns(self, table, row):
            raise FakeAPIError("57014 canceling statement due to statement timeout")

    db = Database(client=Exploding())

    with pytest.raises(FakeAPIError, match="57014"):
        db.upsert_listings([{"item_id": "x"}])


def test_drift_is_learned_once_and_not_retried_forever():
    fake = FakeSupabase(columns={"listings": LIVE_LISTINGS})
    db = Database(client=fake)

    db.upsert_listings([{"item_id": "l3", "title": "a", "country": "ES"}])
    before = len(fake.upserts)
    db.upsert_listings([{"item_id": "l4", "title": "b", "country": "PT"}])

    # Second call strips the known-missing column up front: one attempt, not two.
    assert len(fake.upserts) == before + 1


# ----------------------------------------------------------------------- C5
def test_recent_runs_selects_notes():
    """The dead-man cooldown is a marker written into run_log.notes and read
    back out of the recent history, so the history has to carry the column —
    without it the switch stays stateless and re-fires every run (~1000 identical
    notifications over a weekend outage)."""
    db, fake = make_db(
        run_log=[
            {
                "id": 1,
                "loop_name": "alert",
                "items_seen": 0,
                "alerts_sent": 0,
                "errors": 0,
                "started_at": iso(now()),
                "finished_at": iso(now()),
                "notes": "dead_man_notified",
            }
        ]
    )

    runs = db.recent_runs("alert", 3)

    assert "notes" in fake.select_columns_for("run_log")[0]
    assert runs[0]["notes"] == "dead_man_notified"


def test_recent_runs_ignores_unfinished_runs_and_other_loops():
    db, _ = make_db(
        run_log=[
            {"id": 1, "loop_name": "alert", "started_at": iso(now()), "finished_at": None},
            {"id": 2, "loop_name": "comps", "started_at": iso(now()), "finished_at": iso(now())},
            {"id": 3, "loop_name": "alert", "started_at": iso(now()), "finished_at": iso(now())},
        ]
    )

    assert [r["id"] for r in db.recent_runs("alert", 5)] == [3]


# ------------------------------------------------------------------- purging
def test_purge_deletes_only_rows_past_the_horizon():
    """_purge_before slices by seen_at, which is why observations needs a plain
    seen_at index (C3): neither existing index can serve `order by seen_at
    limit 1` or a bare seen_at range delete."""
    old = iso(now() - timedelta(days=30))
    recent = iso(now() - timedelta(hours=1))
    db, fake = make_db(
        observations=[
            {"id": 1, "seen_at": old},
            {"id": 2, "seen_at": recent},
        ]
    )

    db._purge_before("observations", now() - timedelta(days=7))

    assert [r["id"] for r in fake.rows["observations"]] == [2]


def test_purge_is_a_noop_on_an_empty_table():
    db, fake = make_db(observations=[])

    db._purge_before("observations", now() - timedelta(days=7))

    assert fake.deletes == []


# -------------------------------------------------------------- reserved side
def test_reserved_comps_only_returns_reserved_rows_in_the_window():
    db, _ = make_db(
        observations=[
            {
                "item_id": "o1",
                "model_key": "rtx_3070",
                "status": "reserved",
                "price": 300,
                "seen_at": iso(now() - timedelta(days=2)),
            },
            {
                "item_id": "o2",
                "model_key": "rtx_3070",
                "status": "active",
                "price": 280,
                "seen_at": iso(now()),
            },
            {
                "item_id": "o3",
                "model_key": "rtx_3070",
                "status": "reserved",
                "price": 310,
                "seen_at": iso(now() - timedelta(days=90)),
            },
        ]
    )

    got = db.reserved_comps("rtx_3070", now() - timedelta(days=60))

    assert [r["item_id"] for r in got] == ["o1"]


def test_reserved_comps_are_newest_first():
    """collect_comps takes the first hit per item as its last reserved price."""
    db, _ = make_db(
        observations=[
            {
                "item_id": "o1",
                "model_key": "rtx_3070",
                "status": "reserved",
                "price": 320,
                "seen_at": iso(now() - timedelta(days=5)),
            },
            {
                "item_id": "o1",
                "model_key": "rtx_3070",
                "status": "reserved",
                "price": 290,
                "seen_at": iso(now() - timedelta(days=1)),
            },
        ]
    )

    got = db.reserved_comps("rtx_3070", now() - timedelta(days=60))

    assert [r["price"] for r in got] == [290, 320]


def test_iso_and_now_are_utc():
    assert now().tzinfo is timezone.utc
    assert iso(datetime(2026, 1, 1, tzinfo=timezone.utc)).endswith("+00:00")
