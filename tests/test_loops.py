"""End-to-end wiring for both loops, with the DB, API and Telegram stubbed.

These catch the integration bugs unit tests miss — a dedup rule that never
fires, a reserved listing that gets alerted, a sale inferred one run too early.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import alert_loop  # noqa: E402
import comps_loop  # noqa: E402
import config
import pricing  # noqa: E402
import seed  # noqa: E402
from wallapop_client import Item  # noqa: E402


def make_item(
    item_id,
    title,
    price,
    reserved=False,
    description="",
    condition=None,
    taxonomy=(),
    seller_id=None,
    brand=None,
    posted_at=None,
    modified_at=None,
):
    return Item(
        item_id=item_id,
        title=title,
        description=description,
        price=price,
        currency="EUR",
        web_url=f"https://es.wallapop.com/item/{item_id}",
        image_url="https://cdn/x.jpg",
        reserved=reserved,
        shipping=True,
        location="Madrid",
        distance_km=4.0,
        condition=condition,
        brand=brand,
        taxonomy=taxonomy,
        seller_id=seller_id,
        posted_at=posted_at,
        modified_at=modified_at,
    )


class FakeDB:
    def __init__(
        self,
        searches,
        model_prices=None,
        alerted=None,
        open_listings=None,
        runs=None,
    ):
        self._searches = searches
        self._model_prices = model_prices or {}
        self._alerted = alerted or {}
        self._open = open_listings or []
        self._runs = runs or []
        self.purged = False
        self.junk_purged = False
        self.listings = []
        self.observations = []
        self.recorded = []
        self.junk = []
        self.closed = {}
        self.missing_set = {}
        self.model_writes = []
        self.calls = []
        self.finished = []
        self.sold_comps_calls = []

    # --- alert loop surface
    def start_run(self, name):
        return 1

    def finish_run(self, run_id, **fields):
        # Recorded rather than ignored: the dead-man cooldown works by writing a
        # marker into run_log.notes, so what lands here is the behaviour.
        self.finished.append((run_id, fields))

    def get_searches(self, role):
        return [s for s in self._searches if s["role"] == role]

    def get_model_prices(self):
        return self._model_prices

    def log_junk(self, rows):
        self.junk.extend(rows)

    def upsert_listings(self, rows):
        self.calls.append("upsert_listings")
        self.listings.extend(rows)

    def insert_observations(self, rows):
        # observations.item_id is a FK to listings, so anything written here
        # must already exist in listings or Postgres rejects it with 23503.
        known = {r["item_id"] for r in self.listings}
        for row in rows:
            assert row["item_id"] in known, (
                f"observation for {row['item_id']} written before its listing — "
                "this is the foreign-key violation that crashed production"
            )
        self.observations.extend(rows)

    def get_listings(self, item_ids):
        wanted = set(item_ids)
        return {r["item_id"]: r for r in self.listings if r["item_id"] in wanted}

    def listing_states(self, item_ids):
        self.calls.append("listing_states")
        return {
            k: (v.get("last_price"), v.get("last_status"))
            for k, v in self.get_listings(item_ids).items()
        }

    def insert_changed_observations(self, rows, previous=None):
        """Mirrors Database.insert_changed_observations: only rows whose price
        or status differs from the snapshot taken before the upsert."""
        self.calls.append("insert_changed_observations")
        if previous is None:
            previous = self.listing_states([r["item_id"] for r in rows])
        fresh = []
        for row in rows:
            prior = previous.get(row["item_id"])
            if prior is not None:
                last_price, last_status = prior
                if last_price == row.get("price") and last_status == row.get("status"):
                    continue
            fresh.append(row)
        self.insert_observations(fresh)
        return len(fresh)

    def alerted_prices(self, ids):
        return {k: v for k, v in self._alerted.items() if k in set(ids)}

    def record_alert(self, item_id, price, kind):
        self.recorded.append((item_id, price, kind))

    # --- comps loop surface
    def get_open_listings_for_models(self, keys):
        return [r for r in self._open if r["model_key"] in set(keys)]

    def set_missing_runs(self, item_id, value):
        self.missing_set[item_id] = value

    def mark_closed(self, item_id, price):
        self.closed[item_id] = price

    def last_reserved_price(self, item_id):
        return None

    def reserved_comps(self, model_key, since):
        return []

    def sold_comps(self, model_key, since):
        self.sold_comps_calls.append((model_key, since))
        return []

    def upsert_model_price(self, row):
        self.model_writes.append(row)

    def open_reserved_listings(self, keys):
        return [r for r in self._open if r.get("ever_reserved")]

    def purge_old_observations(self):
        self.purged = True

    def purge_old_junk(self):
        self.junk_purged = True

    def recent_runs(self, loop_name, limit):
        return self._runs[:limit]


class FakeClient:
    """Stands in for WallapopClient in both roles: the search context manager
    and the plain detail client the alert loop keeps open across candidates.

    `details` lets a test give the detail endpoint a *different* payload from
    the search result, which is the only way to exercise fields the search
    response never carries (condition and brand, and a taxonomy that only the
    detail endpoint fills in).
    """

    def __init__(self, items, details=None):
        self._items = items
        self._details = details or {}
        self.detail_calls = []
        self.probed = []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass

    def close(self):
        pass

    def search(self, keywords, **kwargs):
        self.last_search_kwargs = kwargs
        yield from self._items

    def fetch_detail(self, item_id):
        self.detail_calls.append(item_id)
        if item_id in self._details:
            return self._details[item_id]
        for item in self._items:
            if item.item_id == item_id:
                return item
        return None

    def is_alive(self, item_id):
        self.probed.append(item_id)
        return any(i.item_id == item_id for i in self._items)


class FakeTelegram:
    def __init__(self, *a, **k):
        self.sent = []
        self.errors = []

    def send_alert(self, item, deal, kind, previous_price=None, model_display=None):
        self.sent.append((item.item_id, kind, item.price, deal))
        return True

    def send_error(self, text):
        self.errors.append(text)

    def close(self):
        pass


ALERT_SEARCH = {
    "label": "RTX 3070",
    "role": "alert",
    "keywords": "rtx 3070",
    "model_key": "rtx_3070",
    "category_ids": config.CATEGORY_GPU,
    "max_price": 200,
    "distance_km": 100,
}

PRICES = {
    "rtx_3070": {
        "model_key": "rtx_3070",
        "ref_price": 300.0,
        "buy_ceiling": 220.0,
        "buy_ceiling_in_person": 250.0,
        "n_comps": 12,
        "is_seed": False,
    }
}


@pytest.fixture
def wire(monkeypatch):
    """Patch the loops' collaborators and hand back the fakes."""

    def _wire(module, db, items, details=None):
        tg = FakeTelegram()
        monkeypatch.setattr(module, "Database", lambda *a, **k: db)
        monkeypatch.setattr(module, "Telegram", lambda *a, **k: tg)
        monkeypatch.setattr(
            module, "WallapopClient", lambda *a, **k: FakeClient(items, details)
        )
        monkeypatch.setattr(config, "require_secrets", lambda *a: None)
        return tg

    return _wire


# ------------------------------------------------------------------ alerts
def test_cheap_card_alerts_and_is_recorded(wire):
    db = FakeDB([ALERT_SEARCH], PRICES)
    tg = wire(alert_loop, db, [make_item("a1", "RTX 3070 Gigabyte OC", 220.0)])

    stats = alert_loop.run_once()

    assert stats["alerts_sent"] == 1
    assert tg.sent[0][0] == "a1" and tg.sent[0][1] == "new"
    assert db.recorded == [("a1", 220.0, "new")]


def test_dry_run_never_writes_to_the_dedup_table(wire, monkeypatch):
    """Regression: DRY_RUN skips the real Telegram send but FakeTelegram (and
    the real Telegram class) still returns True for logging purposes. If that
    True were treated as a real send, db.record_alert() would mark the item as
    already-alerted — silently suppressing the real alert once this actually
    runs for real. A production incident: local DRY_RUN testing polluted
    sent_alerts with 16 fake rows, none of which ever reached Telegram.
    """
    monkeypatch.setattr(config, "DRY_RUN", True)
    db = FakeDB([ALERT_SEARCH], PRICES)
    tg = wire(alert_loop, db, [make_item("a11", "RTX 3070 Gigabyte OC", 220.0)])

    stats = alert_loop.run_once()

    assert stats["alerts_sent"] == 1   # still visible/counted for the dry-run log
    assert tg.sent[0][0] == "a11"
    assert db.recorded == []           # but never persisted to the dedup table


def test_card_still_above_ceiling_even_after_a_haggle_is_silent(wire):
    # ref=300, ceiling=220. Even a 20%-off offer (280*0.8=224) stays above it.
    db = FakeDB([ALERT_SEARCH], PRICES)
    wire(alert_loop, db, [make_item("a2", "RTX 3070 Asus", 280.0)])
    assert alert_loop.run_once()["alerts_sent"] == 0


def test_reserved_listing_is_never_alerted_but_is_still_observed(wire):
    db = FakeDB([ALERT_SEARCH], PRICES)
    wire(alert_loop, db, [make_item("a3", "RTX 3070 MSI", 150.0, reserved=True)])

    assert alert_loop.run_once()["alerts_sent"] == 0
    # It still prices the market, which is the whole point of tracking reserved.
    assert db.observations[0]["status"] == "reserved"
    assert db.observations[0]["model_key"] == "rtx_3070"


def test_same_listing_same_price_is_not_resent(wire):
    db = FakeDB([ALERT_SEARCH], PRICES, alerted={"a4": [180.0]})
    wire(alert_loop, db, [make_item("a4", "RTX 3070 Gigabyte", 220.0)])
    assert alert_loop.run_once()["alerts_sent"] == 0


def test_price_drop_resends(wire):
    db = FakeDB([ALERT_SEARCH], PRICES, alerted={"a5": [240.0]})
    tg = wire(alert_loop, db, [make_item("a5", "RTX 3070 Gigabyte", 200.0)])

    assert alert_loop.run_once()["alerts_sent"] == 1
    assert tg.sent[0][1] == "price_drop"


def test_price_increase_does_not_resend(wire):
    db = FakeDB([ALERT_SEARCH], PRICES, alerted={"a6": [160.0]})
    wire(alert_loop, db, [make_item("a6", "RTX 3070 Gigabyte", 220.0)])
    assert alert_loop.run_once()["alerts_sent"] == 0


def test_junk_is_filtered_and_logged(wire):
    db = FakeDB([ALERT_SEARCH], PRICES)
    wire(alert_loop, db, [make_item("a7", "RTX 3070 no funciona para piezas", 60.0)])

    stats = alert_loop.run_once()
    assert stats["alerts_sent"] == 0
    assert db.junk[0]["category"] == "DEFECT"


def test_laptop_with_a_matching_gpu_never_alerts(wire):
    db = FakeDB([ALERT_SEARCH], PRICES)
    wire(alert_loop, db, [make_item("a8", "Lenovo Legion 5 RTX 3070", 220.0)])

    assert alert_loop.run_once()["alerts_sent"] == 0
    assert db.junk[0]["category"] == "LAPTOP"


def test_irrelevant_result_under_the_cap_is_dropped(wire):
    """Wallapop's loose matching returns CPUs for a GPU search."""
    db = FakeDB([ALERT_SEARCH], PRICES)
    wire(alert_loop, db, [make_item("a9", "RTX 4060 Gigabyte OC", 220.0)])

    assert alert_loop.run_once()["alerts_sent"] == 0
    assert db.junk == []  # not junk, just not what we searched for


def test_card_qualifies_via_offer_even_when_asking_is_above_the_ceiling(wire):
    """The offer-based gate: asking can sit above the raw ceiling and still
    qualify, as long as a realistic 20% haggle would clear it."""
    search = dict(ALERT_SEARCH, label="RTX 3070", keywords="rtx 3070",
                  model_key="rtx_3070", max_price=None)
    prices = {"rtx_3070": {"ref_price": 300.0, "buy_ceiling": 220.0,
                           "buy_ceiling_in_person": 250.0, "n_comps": 9, "is_seed": False}}
    db = FakeDB([search], prices)
    tg = wire(alert_loop, db, [make_item("a10", "RTX 3070 Gigabyte", 270.0)])
    # asking 270 > ceiling 220, but offer 270*0.8=216 <= 220 -> qualifies.
    assert alert_loop.run_once()["alerts_sent"] == 1
    assert tg.sent[0][2] == 270.0


def test_expensive_card_never_alerts_however_good_the_margin(wire):
    """A price cap on the asking price outranks the margin verdict. A 5080 at
    690 with a 780 ceiling clears the offer gate comfortably and still must not
    fire — capital at risk on one purchase is a separate question from whether
    the trade is good, which is what MAX_CAPITAL_PRICE answers now that a priced
    listing is no longer held to the bootstrap ceiling."""
    search = dict(ALERT_SEARCH, label="RTX 5080", keywords="rtx 5080",
                  model_key="rtx_5080", max_price=None)
    prices = {"rtx_5080": {"ref_price": 900.0, "buy_ceiling": 780.0,
                           "buy_ceiling_in_person": 830.0, "n_comps": 9, "is_seed": False}}
    db = FakeDB([search], prices)
    tg = wire(alert_loop, db, [make_item("a11", "RTX 5080 Asus", 690.0)])

    stats = alert_loop.run_once()
    assert stats["alerts_sent"] == 0
    assert stats["over_cap"] == 1
    assert tg.sent == []


def test_underpriced_high_end_card_still_gets_through_the_cap(wire):
    """The flip side, and the whole reason the cap is on price rather than on
    model tier: a 4090 well under the cap is exactly what we want to catch, and
    the cap must not reject it for being a high-tier model."""
    search = dict(ALERT_SEARCH, label="RTX 4080", keywords="rtx 4080",
                  model_key="rtx_4080", max_price=None)
    prices = {"rtx_4080": {"ref_price": 620.0, "buy_ceiling": 525.0,
                           "buy_ceiling_in_person": 570.0, "n_comps": 9, "is_seed": False}}
    db = FakeDB([search], prices)
    tg = wire(alert_loop, db, [make_item("a12", "RTX 4080 Gigabyte", 420.0)])

    assert alert_loop.run_once()["alerts_sent"] == 1
    assert tg.sent[0][2] == 420.0


def test_priced_listing_above_the_bootstrap_cap_is_still_evaluated(wire):
    """The trade the flat 350 EUR cap was throwing away by construction.

    A 4080 at 420 against a 620 reference has to clear a 112 EUR required margin
    and a ~468 EUR shipped buy ceiling — a better trade than anything a 350 cap
    could admit — and it was dropped in the pre-filter without `pricing.evaluate`
    ever seeing it. Once a model has a reference price the margin gate is the
    real test, so the cap that applies is MAX_CAPITAL_PRICE.
    """
    search = dict(ALERT_SEARCH, label="RTX 4080", keywords="rtx 4080",
                  model_key="rtx_4080", max_price=None)
    prices = {"rtx_4080": {"ref_price": 620.0, "buy_ceiling": 525.0,
                           "buy_ceiling_in_person": 570.0, "n_comps": 9, "is_seed": False}}
    db = FakeDB([search], prices)
    tg = wire(alert_loop, db, [make_item("cap1", "RTX 4080 Gigabyte", 420.0)])

    stats = alert_loop.run_once()
    assert stats["alerts_sent"] == 1
    assert stats["over_cap"] == 0
    assert tg.sent[0][2] == 420.0


def test_capital_cap_still_binds_on_a_priced_listing(wire):
    """The raised cap is a raised cap, not the absence of one: past
    MAX_CAPITAL_PRICE the answer is no regardless of the margin, because the
    question there is how much goes into one card."""
    search = dict(ALERT_SEARCH, label="RTX 5080", keywords="rtx 5080",
                  model_key="rtx_5080", max_price=None)
    prices = {"rtx_5080": {"ref_price": 900.0, "buy_ceiling": 780.0,
                           "buy_ceiling_in_person": 830.0, "n_comps": 9, "is_seed": False}}
    db = FakeDB([search], prices)
    tg = wire(alert_loop, db, [make_item("cap2", "RTX 5080 Asus", 750.0)])

    stats = alert_loop.run_once()
    assert stats["alerts_sent"] == 0
    assert stats["over_cap"] == 1
    assert tg.sent == []


def test_listing_with_no_reference_price_keeps_the_low_bootstrap_cap(wire):
    """The bootstrap path is the one MAX_ALERT_PRICE still governs, and it must
    not inherit the generous capital cap. With no learned price there is nothing
    but a keyword match behind the alert, so the search's own 600 EUR bootstrap
    cap is not sufficient protection — 500 EUR of capital on an unpriced guess
    is exactly what the flat ceiling exists to refuse. MAX_ALERT_PRICE is 450
    since 2026-08-26.
    """
    search = dict(ALERT_SEARCH, label="RTX 3070", keywords="rtx 3070",
                  model_key="rtx_3070", max_price=600)
    db = FakeDB([search], {})  # nothing learned, nothing seeded
    tg = wire(alert_loop, db, [make_item("cap3", "RTX 3070 Asus Dual", 500.0)])

    stats = alert_loop.run_once()
    assert stats["alerts_sent"] == 0
    assert stats["over_cap"] == 1
    assert tg.sent == []


def test_whole_machine_cannot_alert_through_the_raised_cap(wire):
    """The second job the flat 350 EUR cap was quietly doing.

    A prebuilt or a gaming laptop is essentially never listed under 350, so the
    old cap kept whole machines out of the feed without identifying them.
    MAX_CAPITAL_PRICE at 700 is squarely inside prebuilt territory, so form
    factor now has to be rejected on purpose — off the `whole_machine` taxonomy
    flag. This listing would otherwise sail through: 420 EUR against a 620
    reference is a qualifying margin.
    """
    search = dict(ALERT_SEARCH, label="RTX 4080", keywords="rtx 4080",
                  model_key="rtx_4080", max_price=None)
    prices = {"rtx_4080": {"ref_price": 620.0, "buy_ceiling": 525.0,
                           "buy_ceiling_in_person": 570.0, "n_comps": 9, "is_seed": False}}
    db = FakeDB([search], prices)
    tg = wire(
        alert_loop,
        db,
        [make_item("pc2", "RTX 4080 sobremesa", 420.0, taxonomy=(24200, 24115))],
    )

    stats = alert_loop.run_once()
    assert stats["alerts_sent"] == 0
    assert stats["whole_machine"] == 1
    assert tg.sent == []


def test_whole_machine_revealed_only_by_the_detail_payload_is_blocked(wire):
    """Some search results arrive without a taxonomy; the detail endpoint is the
    authoritative one, so form factor is re-checked after enrichment rather than
    trusted once."""
    search = dict(ALERT_SEARCH, label="RTX 4080", keywords="rtx 4080",
                  model_key="rtx_4080", max_price=None)
    prices = {"rtx_4080": {"ref_price": 620.0, "buy_ceiling": 525.0,
                           "buy_ceiling_in_person": 570.0, "n_comps": 9, "is_seed": False}}
    db = FakeDB([search], prices)
    tg = wire(
        alert_loop,
        db,
        [make_item("pc3", "RTX 4080 Gigabyte OC", 420.0, taxonomy=())],
        details={"pc3": make_item("pc3", "RTX 4080 Gigabyte OC", 420.0,
                                  taxonomy=(24116,))},
    )

    stats = alert_loop.run_once()
    assert stats["alerts_sent"] == 0
    assert stats["whole_machine"] == 1
    assert tg.sent == []


# ------------------------------------------------------------ blocked sellers
def test_blocked_seller_never_alerts(wire, monkeypatch):
    """The alert dedup is keyed on (item_id, price), which a serial relister
    defeats completely: the same replica card or empty box comes back under a
    fresh item_id every few days and alerts as brand new every time. The seller
    id is the only identifier that survives the relisting."""
    monkeypatch.setattr(config, "BLOCKED_SELLERS", frozenset({"u666"}))
    db = FakeDB([ALERT_SEARCH], PRICES)
    tg = wire(
        alert_loop,
        db,
        [
            make_item("bs1", "RTX 3070 Gigabyte", 220.0, seller_id="u666"),
            make_item("bs2", "RTX 3070 Asus", 220.0, seller_id="u1"),
        ],
    )

    stats = alert_loop.run_once()
    assert stats["blocked_seller"] == 1
    assert [s[0] for s in tg.sent] == ["bs2"]


def test_blocked_seller_listing_is_still_recorded(wire, monkeypatch):
    """Blocking is about the alert feed, not about the data: the listing still
    prices the market and still has to be visible for tuning the block list."""
    monkeypatch.setattr(config, "BLOCKED_SELLERS", frozenset({"u666"}))
    db = FakeDB([ALERT_SEARCH], PRICES)
    wire(alert_loop, db, [make_item("bs3", "RTX 3070 Gigabyte", 220.0, seller_id="u666")])

    alert_loop.run_once()
    assert db.listings[0]["seller_id"] == "u666"
    assert db.observations[0]["item_id"] == "bs3"


def test_seller_id_is_persisted_by_both_loops(wire):
    """A block list can only be maintained from ids the database actually holds,
    so both loops have to write the column."""
    adb = FakeDB([ALERT_SEARCH], PRICES)
    wire(alert_loop, adb, [make_item("sid1", "RTX 3070 Gigabyte", 220.0, seller_id="u42")])
    alert_loop.run_once()

    cdb = FakeDB([dict(ALERT_SEARCH, role="comps", label="Comps RTX 3070")], PRICES)
    wire(comps_loop, cdb, [make_item("sid1", "RTX 3070 Gigabyte", 220.0, seller_id="u42")])
    comps_loop.run_once()

    assert adb.listings[0]["seller_id"] == "u42"
    assert cdb.listings[0]["seller_id"] == "u42"


def test_an_extremely_cheap_listing_is_now_rejected(wire):
    """The owner's decision, reversed again on 2026-08-26.

    MIN_PLAUSIBLE_RATIO was 0.35, then 0 (send everything that clears the margin
    and let a person judge legitimacy from the photos), and is now 0.65 — the
    strictest it has ever been. Live experience decided it: fakes reaching the
    feed cost more than the missed steals did.

    A 4090 at 340 against a 1200 reference is 28% of the reference. It clears
    the margin gate by a mile and is refused anyway, without being evaluated —
    which is precisely the trade being made here, and precisely where the
    biggest genuine wins used to come from.

    Set MIN_PLAUSIBLE_RATIO=0 to restore the previous behaviour; this test then
    fails, which is the point — it pins a policy choice, not an accident.
    """
    search = dict(ALERT_SEARCH, label="RTX 4090", keywords="rtx 4090",
                  model_key="rtx_4090", max_price=None)
    prices = {"rtx_4090": {"ref_price": 1200.0, "buy_ceiling": 1050.0,
                           "buy_ceiling_in_person": 1150.0, "n_comps": 9, "is_seed": False}}
    db = FakeDB([search], prices)
    tg = wire(alert_loop, db, [make_item("a13", "RTX 4090 Gigabyte", 340.0)])

    assert alert_loop.run_once()["alerts_sent"] == 0
    assert tg.sent == []


def test_a_listing_just_above_the_plausibility_floor_is_sent(wire):
    """The counterpart, so the test above cannot pass by the feed being broken.
    On a 620 EUR reference the floor is 403 and the capital cap is 500, so 480
    clears the floor, the cap and the buy ceiling."""
    search = dict(ALERT_SEARCH, label="RTX 4080", keywords="rtx 4080",
                  model_key="rtx_4080", max_price=None)
    prices = {"rtx_4080": {"ref_price": 620.0, "buy_ceiling": 525.0,
                           "buy_ceiling_in_person": 570.0, "n_comps": 9, "is_seed": False}}
    db = FakeDB([search], prices)
    tg = wire(alert_loop, db, [make_item("a15", "RTX 4080 Gigabyte", 480.0)])

    assert alert_loop.run_once()["alerts_sent"] == 1
    assert len(tg.sent) == 1


def test_the_floor_and_the_capital_cap_leave_a_dead_zone_for_top_cards():
    """Two settings that are each reasonable alone and interact badly.

    MIN_PLAUSIBLE_RATIO refuses anything under 65% of the reference; the capital
    cap refuses anything over MAX_CAPITAL_PRICE. Above a reference of
    MAX_CAPITAL_PRICE / MIN_PLAUSIBLE_RATIO the floor rises *above* the cap and
    the allowed window closes completely, so the model can never alert at any
    price whatsoever.

    At the 2026-08-26 settings (500 / 0.65) that breakeven is ~769 EUR. The RTX
    4090 and 5090 were deleted from the registry rather than left in it dead,
    but the RTX 5080 (seed 900) is still tracked and still inside the dead zone:
    it can learn a price and will never alert on one. This is a consequence, not
    a decision — the test exists so it stays visible and fails loudly if either
    number moves.
    """
    breakeven = config.MAX_CAPITAL_PRICE / config.MIN_PLAUSIBLE_RATIO
    for ref in (900.0,):  # rtx_5080 seed reference
        assert ref > breakeven
        floor = ref * config.MIN_PLAUSIBLE_RATIO
        assert floor > config.MAX_CAPITAL_PRICE
        row = {"ref_price": ref, "buy_ceiling": ref * 0.85,
               "buy_ceiling_in_person": ref * 0.93, "n_comps": 9}
        # Everything the cap allows is below the floor.
        assert not pricing.evaluate(config.MAX_CAPITAL_PRICE, row, None).qualifies


def test_nothing_under_the_sanity_floor_is_ever_sent(wire):
    """The absolute floor, below the plausibility ratio and independent of it.
    A 49 EUR listing is refused on the sanity band alone, before any reference
    price is consulted at all.
    """
    search = dict(ALERT_SEARCH, label="RTX 4090", keywords="rtx 4090",
                  model_key="rtx_4090", max_price=None)
    prices = {"rtx_4090": {"ref_price": 1200.0, "buy_ceiling": 1050.0,
                           "buy_ceiling_in_person": 1150.0, "n_comps": 9, "is_seed": False}}
    db = FakeDB([search], prices)
    tg = wire(alert_loop, db, [make_item("a14", "RTX 4090 Gigabyte", 49.0)])

    assert alert_loop.run_once()["alerts_sent"] == 0
    assert tg.sent == []


def test_bottom_condition_tier_is_blocked_but_fair_is_not(wire):
    """The dead-card filter. Only `has_given_it_all` is blocked — `fair` is a
    working card with a cosmetic flaw, which is exactly what a flip is."""
    db = FakeDB([ALERT_SEARCH], PRICES)
    tg = wire(
        alert_loop,
        db,
        [
            make_item("dead", "RTX 3070 Gigabyte", 220.0, condition="has_given_it_all"),
            make_item("worn", "RTX 3070 Asus Dual", 220.0, condition="fair"),
        ],
    )

    stats = alert_loop.run_once()
    assert stats["blocked_condition"] == 1
    assert [s[0] for s in tg.sent] == ["worn"]


def test_whole_machine_price_never_enters_a_comps_pool(wire):
    """A prebuilt that names a card is a real listing but not a comp for the
    loose card — its observation must carry no model_key."""
    db = FakeDB([ALERT_SEARCH], PRICES)
    wire(
        alert_loop,
        db,
        [make_item("pc1", "RTX 3070 Gigabyte", 300.0, taxonomy=(24200, 24203, 24115, 24117))],
    )

    alert_loop.run_once()
    assert [o["model_key"] for o in db.observations] == [None]


# ------------------------------------------------------- persistence contract
def test_alert_loop_marks_ever_reserved(wire):
    """The fastest sales were being lost by the loop best placed to catch them.

    `ever_reserved` is sticky, and the alert loop's row builder omitted the
    column entirely. `infer_sales` falls back to `last_status == 'reserved'` so
    the common case survived, but a listing that goes reserved, un-reserves and
    then vanishes closes with `sold_price = None` and yields no comp at all. The
    5-minute loop is the one that sees short-lived listings, and a short-lived
    listing is usually one that sold.
    """
    db = FakeDB([ALERT_SEARCH], PRICES)
    wire(
        alert_loop,
        db,
        [
            make_item("er1", "RTX 3070 MSI", 150.0, reserved=True),
            make_item("er2", "RTX 3070 Asus Dual", 150.0),
        ],
    )

    alert_loop.run_once()
    rows = {r["item_id"]: r for r in db.listings}
    assert rows["er1"]["ever_reserved"] is True
    # PostgREST only updates the columns present in the payload, so a
    # non-reserved row must *omit* the column rather than send False — sending
    # False would wipe the stickiest fact a listing has.
    assert "ever_reserved" not in rows["er2"]


def test_both_loops_write_the_same_listing_columns(wire):
    """The two row builders drifted once — only the alert loop wrote `country` —
    so a listing's column set depended on which loop happened to see it. They
    are one function now; this pins that they stay one."""
    adb = FakeDB([ALERT_SEARCH], PRICES)
    wire(alert_loop, adb, [make_item("cols1", "RTX 3070 Gigabyte OC", 220.0)])
    alert_loop.run_once()

    cdb = FakeDB([dict(ALERT_SEARCH, role="comps", label="Comps RTX 3070")], PRICES)
    wire(comps_loop, cdb, [make_item("cols1", "RTX 3070 Gigabyte OC", 220.0)])
    comps_loop.run_once()

    assert set(adb.listings[0]) == set(cdb.listings[0])
    for column in ("country", "seller_id", "modified_at"):
        assert column in adb.listings[0]


def test_modified_at_is_persisted(wire):
    """`Item.modified_at` was parsed and read by nothing. It is the only signal
    for 'the seller just edited or cut the price' on a listing that never
    alerted, so it has to reach the database to be usable at all."""
    edited = datetime.now(timezone.utc) - timedelta(hours=2)
    db = FakeDB([ALERT_SEARCH], PRICES)
    wire(
        alert_loop,
        db,
        [make_item("m1", "RTX 3070 Gigabyte OC", 220.0, modified_at=edited)],
    )

    alert_loop.run_once()
    assert db.listings[0]["modified_at"] is not None
    assert db.listings[0]["modified_at"].startswith(str(edited.year))


def test_enriched_detail_fields_are_written_back(wire):
    """Everything `_enrich` paid a request for used to be dropped on the floor.

    The listings upsert happens before the alert pass, so the condition, brand,
    seller-shipping flag and detail taxonomy were used for the caption and the
    block check and then discarded — no condition value ever reached the
    database, which is why the dashboard had none to show and 'which conditions
    produced good buys' was unanswerable.
    """
    db = FakeDB([ALERT_SEARCH], PRICES)
    wire(
        alert_loop,
        db,
        [make_item("en1", "RTX 3070 Gigabyte OC", 220.0)],
        details={
            "en1": make_item(
                "en1", "RTX 3070 Gigabyte OC", 180.0, condition="good", brand="Gigabyte"
            )
        },
    )

    alert_loop.run_once()
    rows = [r for r in db.listings if r["item_id"] == "en1"]
    assert len(rows) == 2, "the enriched row must be written back after the alert pass"
    assert rows[0]["condition"] is None      # search response carries none
    assert rows[-1]["condition"] == "good"   # detail response does
    assert rows[-1]["brand"] == "Gigabyte"
    # Candidates are non-reserved by construction, so the write-back must not
    # mention the sticky column.
    assert "ever_reserved" not in rows[-1]


def test_enriched_write_back_is_one_batch(wire):
    """A handful of rows per run, but one write, not one per listing."""
    db = FakeDB([ALERT_SEARCH], PRICES)
    wire(
        alert_loop,
        db,
        [
            make_item("eb1", "RTX 3070 Gigabyte OC", 220.0),
            make_item("eb2", "RTX 3070 Asus Dual", 175.0),
            make_item("eb3", "RTX 3070 MSI Ventus", 170.0),
        ],
    )

    alert_loop.run_once()
    # One batch for the search results, one for the three enriched rows.
    assert db.calls.count("upsert_listings") == 2


# ------------------------------------------------------------- sale inference
def test_reserved_item_needs_two_misses_before_counting_as_sold():
    db = FakeDB(
        [],
        open_listings=[
            {
                "item_id": "s1",
                "model_key": "rtx_3070",
                "last_price": 200,
                "last_status": "reserved",
                "ever_reserved": True,
                "missing_runs": 0,
                "title": "RTX 3070",
            }
        ],
    )
    # First miss: counted, not closed.
    assert comps_loop.infer_sales(db, {"rtx_3070"}, seen_ids=set()) == 0
    assert db.missing_set["s1"] == 1
    assert db.closed == {}

    db._open[0]["missing_runs"] = 1
    assert comps_loop.infer_sales(db, {"rtx_3070"}, seen_ids=set()) == 1
    assert db.closed["s1"] == 200


def test_item_seen_again_resets_its_miss_counter():
    db = FakeDB(
        [],
        open_listings=[
            {
                "item_id": "s2",
                "model_key": "rtx_3070",
                "last_price": 200,
                "last_status": "reserved",
                "ever_reserved": True,
                "missing_runs": 1,
                "title": "RTX 3070",
            }
        ],
    )
    comps_loop.infer_sales(db, {"rtx_3070"}, seen_ids={"s2"})
    assert db.missing_set["s2"] == 0
    assert db.closed == {}


def test_split_vram_children_are_covered_by_a_generic_comps_search(wire):
    """Regression: a generic 'rtx 4060 ti' search must also cover its 8g/16g
    children. classify() can resolve a listing to rtx_4060_ti_8g even though
    only the generic key has a search row — without expanding the covered set
    through GENERIC_FALLBACKS, that listing's closure (and its sold comp)
    would never be detected.
    """
    search = {
        "label": "Comps RTX 4060 TI",
        "role": "comps",
        "keywords": "rtx 4060 ti",
        "model_key": "rtx_4060_ti",
        "category_ids": config.CATEGORY_GPU,
        "max_price": None,
    }
    db = FakeDB(
        [search],
        open_listings=[
            {
                "item_id": "c1",
                "model_key": "rtx_4060_ti_8g",
                "last_price": 260,
                "last_status": "reserved",
                "ever_reserved": True,
                "missing_runs": 1,  # one miss already recorded last cycle
                "title": "RTX 4060 Ti 8GB",
            }
        ],
    )
    wire(comps_loop, db, [])  # nothing returned this cycle -> c1 stays absent

    stats = comps_loop.run_once()

    assert stats["closed"] == 1
    assert db.closed["c1"] == 260


def test_never_reserved_item_closes_without_a_sold_price():
    """Vanishing without ever being reserved tells us nothing about price."""
    db = FakeDB(
        [],
        open_listings=[
            {
                "item_id": "s3",
                "model_key": "rtx_3070",
                "last_price": 200,
                "last_status": "active",
                "ever_reserved": False,
                "missing_runs": 1,
                "title": "RTX 3070",
            }
        ],
    )
    assert comps_loop.infer_sales(db, {"rtx_3070"}, seen_ids=set()) == 1
    assert db.closed["s3"] is None


# --------------------------------------------------------- liveness probing
def _open_row(item_id, ever_reserved, missing_runs=0, status="reserved"):
    return {
        "item_id": item_id,
        "model_key": "rtx_3070",
        "last_price": 200,
        "last_status": status,
        "ever_reserved": ever_reserved,
        "missing_runs": missing_runs,
        "title": "RTX 3070",
    }


def test_only_reserved_listings_are_probed(monkeypatch):
    """The probe budget goes where a probe can change an outcome.

    Every open listing missing from a run used to get a detail request, uncapped
    and with no delay — each one able to spend up to 14s in retry backoff on a
    403, which is both the likeliest way to hit the 70-minute workflow timeout
    and the fastest way to earn a rate-limit. A never-reserved listing cannot
    produce a sold comp in any case: `mark_closed(item_id, None)` is all it can
    ever yield, so confirming it is gone buys nothing the missing_runs counter
    doesn't already cover.
    """
    monkeypatch.setattr(config, "REQUEST_DELAY", 0)
    db = FakeDB(
        [],
        open_listings=[_open_row("p1", True, 1), _open_row("p2", False, 1, "active")],
    )
    wp = FakeClient([])  # nothing alive

    comps_loop.infer_sales(db, {"rtx_3070"}, seen_ids=set(), wp=wp)

    assert wp.probed == ["p1"]
    assert db.closed["p1"] == 200   # confirmed gone, and it was reserved
    assert db.closed["p2"] is None  # counter path, closed but priceless


def test_currently_reserved_listing_is_probed_even_without_the_sticky_flag(monkeypatch):
    """The probe set has to match the `was_reserved` test, not just the flag.

    `ever_reserved` is NULL on every listing the alert loop wrote before it
    started setting the column, so `open_reserved_listings` alone would have
    dropped those from probing until the backlog aged out — even though
    `last_status == 'reserved'` makes them able to produce a sold comp today.
    """
    monkeypatch.setattr(config, "REQUEST_DELAY", 0)
    row = _open_row("nf1", None, 1)  # reserved now, flag never written
    db = FakeDB([], open_listings=[row])
    wp = FakeClient([])

    comps_loop.infer_sales(db, {"rtx_3070"}, seen_ids=set(), wp=wp)

    assert wp.probed == ["nf1"]
    assert db.closed["nf1"] == 200


def test_liveness_probes_are_capped_per_run(monkeypatch):
    """Past the cap, listings fall back to the missing_runs counter rather than
    the run spending an unbounded number of requests."""
    monkeypatch.setattr(config, "REQUEST_DELAY", 0)
    monkeypatch.setattr(comps_loop, "MAX_LIVENESS_PROBES", 1)
    db = FakeDB(
        [],
        open_listings=[_open_row(f"q{i}", True, 0) for i in range(3)],
    )
    wp = FakeClient([])

    comps_loop.infer_sales(db, {"rtx_3070"}, seen_ids=set(), wp=wp)

    assert len(wp.probed) == 1
    # The two unprobed ones took the counter path: first miss, not yet closed.
    assert db.missing_set == {"q1": 1, "q2": 1}
    assert list(db.closed) == ["q0"]


def test_liveness_probes_pause_between_requests(monkeypatch):
    """The search path sleeps REQUEST_DELAY between pages; this path did not
    sleep at all, which is what made it the impolite one."""
    slept = []
    monkeypatch.setattr(comps_loop.time, "sleep", slept.append)
    monkeypatch.setattr(config, "REQUEST_DELAY", 0.25)
    db = FakeDB([], open_listings=[_open_row("r1", True, 1), _open_row("r2", True, 1)])
    wp = FakeClient([])

    comps_loop.infer_sales(db, {"rtx_3070"}, seen_ids=set(), wp=wp)

    assert len(wp.probed) == 2
    assert slept == [0.25], "one pause, between the two probes"


def test_a_listing_seen_this_run_is_never_probed(monkeypatch):
    """Present in the results is already proof of life; a request would be pure
    waste."""
    monkeypatch.setattr(config, "REQUEST_DELAY", 0)
    db = FakeDB([], open_listings=[_open_row("s9", True, 1)])
    wp = FakeClient([])

    comps_loop.infer_sales(db, {"rtx_3070"}, seen_ids={"s9"}, wp=wp)

    assert wp.probed == []
    assert db.missing_set["s9"] == 0


# ------------------------------------------------------- redundant round trips
def test_sold_comps_is_queried_once_per_model(wire):
    """The same query ran twice per model — once directly for median
    days-to-sale and once inside pricing.collect_comps — for ~80 redundant round
    trips a run. Generic keys made it worse: a split-VRAM sibling was queried
    once as its own target and again when the generic key borrowed from it.
    """
    search = {
        "label": "Comps RTX 4060 TI",
        "role": "comps",
        "keywords": "rtx 4060 ti",
        "model_key": "rtx_4060_ti",
        "category_ids": config.CATEGORY_GPU,
        "max_price": None,
    }
    db = FakeDB([search], {})
    wire(comps_loop, db, [make_item("dd1", "RTX 4060 Ti 8GB", 260.0)])

    comps_loop.run_once()

    keys = [k for k, _ in db.sold_comps_calls]
    assert keys, "the repricing pass must still read the sold rows"
    assert len(keys) == len(set(keys)), f"sold_comps queried a model twice: {keys}"


# ------------------------------------------------- storage-growth regressions
def test_unchanged_listing_writes_no_second_observation(wire):
    """The bug that exhausted the database, in miniature. An observation exists
    to capture a change; writing one every pass meant a listing sitting
    untouched for weeks produced thousands of identical rows that the comps
    pool then deduped straight back down to one price per item."""
    db = FakeDB([ALERT_SEARCH], PRICES)
    item = make_item("s1", "RTX 3070 Gigabyte OC", 220.0)
    wire(alert_loop, db, [item])

    alert_loop.run_once()
    first = len(db.observations)
    assert first == 1

    alert_loop.run_once()          # same listing, same price, same status
    assert len(db.observations) == first, "an unchanged listing must not re-observe"


def test_a_price_change_still_records_an_observation(wire):
    db = FakeDB([ALERT_SEARCH], PRICES)
    wire(alert_loop, db, [make_item("s2", "RTX 3070 Gigabyte OC", 220.0)])
    alert_loop.run_once()
    assert len(db.observations) == 1

    wire(alert_loop, db, [make_item("s2", "RTX 3070 Gigabyte OC", 150.0)])
    alert_loop.run_once()
    assert len(db.observations) == 2
    assert db.observations[-1]["price"] == 150.0


def test_a_status_change_still_records_an_observation(wire):
    db = FakeDB([ALERT_SEARCH], PRICES)
    wire(alert_loop, db, [make_item("s3", "RTX 3070 Gigabyte OC", 220.0)])
    alert_loop.run_once()

    wire(alert_loop, db, [make_item("s3", "RTX 3070 Gigabyte OC", 220.0, reserved=True)])
    alert_loop.run_once()
    assert db.observations[-1]["status"] == "reserved"


def test_comps_loop_sweeps_both_retention_tables(wire):
    """junk_exclusions is the table that actually blew the quota — 2.86M rows
    in 16 days — so its sweep has to run, not just the observations one."""
    search = dict(ALERT_SEARCH, role="comps", label="Comps RTX 3070")
    db = FakeDB([search], PRICES)
    wire(comps_loop, db, [make_item("c1", "RTX 3070 Gigabyte OC", 220.0)])

    comps_loop.run_once()
    assert db.purged, "observations retention did not run"
    assert db.junk_purged, "junk retention did not run"


def test_listings_are_written_before_their_observations(wire):
    """Production crash regression (PostgREST 23503). observations.item_id is a
    foreign key to listings, so a brand-new listing must be upserted before its
    observation. The change comparison needs the *old* state though, so the
    order has to be: snapshot states, upsert listings, then observe."""
    db = FakeDB([ALERT_SEARCH], PRICES)
    wire(alert_loop, db, [make_item("fk1", "RTX 3070 Gigabyte OC", 220.0)])

    alert_loop.run_once()

    order = [c for c in db.calls if c in
             ("listing_states", "upsert_listings", "insert_changed_observations")]
    assert order.index("listing_states") < order.index("upsert_listings")
    assert order.index("upsert_listings") < order.index("insert_changed_observations")
    assert len(db.observations) == 1


# ------------------------------------------------------------ dead-man switch
def _empty_run():
    return {"items_seen": 0, "alerts_sent": 0, "errors": 0, "finished_at": "x"}


def test_dead_man_fires_after_consecutive_empty_runs(wire):
    """The comps loop returned 0 items on all 40 searches, hourly, for over a
    day, and nothing surfaced it — because only the alert loop was covered."""
    db = FakeDB([ALERT_SEARCH], PRICES, runs=[_empty_run()] * config.DEAD_MAN_RUNS)
    tg = wire(alert_loop, db, [])

    alert_loop.run_once()
    assert tg.errors, "a run of empty passes must raise the alarm"
    assert "zero" in tg.errors[0].lower()


def test_dead_man_stays_quiet_while_items_are_flowing(wire):
    healthy = dict(_empty_run(), items_seen=120)
    db = FakeDB([ALERT_SEARCH], PRICES, runs=[healthy] * config.DEAD_MAN_RUNS)
    tg = wire(alert_loop, db, [make_item("d1", "RTX 3070 Gigabyte OC", 220.0)])

    alert_loop.run_once()
    assert tg.errors == []


def test_comps_loop_is_covered_by_the_dead_man_switch(wire):
    """The regression that actually happened: the check existed, but only the
    alert loop called it."""
    search = dict(ALERT_SEARCH, role="comps", label="Comps RTX 3070")
    db = FakeDB([search], PRICES, runs=[_empty_run()] * config.DEAD_MAN_RUNS)
    tg = wire(comps_loop, db, [])

    comps_loop.run_once()
    assert tg.errors, "comps must raise the alarm too"
    assert "comps" in tg.errors[0]


def _marked_run(hours_ago):
    """An empty run whose notes carry the dead-man marker."""
    stamp = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return dict(
        _empty_run(),
        notes=f"junk_filtered=0 over_cap=0 {alert_loop.DEAD_MAN_MARKER}",
        started_at=stamp.isoformat(),
    )


def test_dead_man_warning_is_marked_in_the_run_log(wire):
    """The cooldown has to be stateless — every pass is a fresh Actions runner
    with no memory of the last one — so the marker goes into run_log.notes,
    which is state the check already reads."""
    db = FakeDB([ALERT_SEARCH], PRICES, runs=[_empty_run()] * config.DEAD_MAN_RUNS)
    tg = wire(alert_loop, db, [])

    alert_loop.run_once()
    assert tg.errors
    written = [f.get("notes", "") for _, f in db.finished]
    assert any(alert_loop.DEAD_MAN_MARKER in n for n in written)


def test_dead_man_stays_quiet_when_a_recent_run_already_warned(wire):
    """A weekend outage at the 5-minute cadence is ~1000 identical messages,
    which is exactly how an alert channel stops being read — and an unread
    channel is the same outage the switch exists to prevent."""
    db = FakeDB(
        [ALERT_SEARCH],
        PRICES,
        runs=[_empty_run(), _marked_run(1), _empty_run()],
    )
    tg = wire(alert_loop, db, [])

    alert_loop.run_once()
    assert tg.errors == [], "a warning already went out inside the cooldown"


def test_dead_man_warns_again_once_the_cooldown_expires(wire):
    """Suppression, not silence: a still-broken bot has to keep saying so, just
    not every five minutes."""
    stale = _marked_run(alert_loop.DEAD_MAN_COOLDOWN_HOURS + 2)
    db = FakeDB([ALERT_SEARCH], PRICES, runs=[_empty_run(), stale, _empty_run()])
    tg = wire(alert_loop, db, [])

    alert_loop.run_once()
    assert tg.errors, "past the cooldown the alarm must sound again"


def test_dead_man_cooldown_does_not_mask_a_healthy_loop(wire):
    """A marker from an old outage must not suppress anything once items flow —
    the streak check still gates everything."""
    healthy = dict(_empty_run(), items_seen=120)
    db = FakeDB([ALERT_SEARCH], PRICES, runs=[healthy, _marked_run(1), healthy])
    tg = wire(alert_loop, db, [make_item("dm1", "RTX 3070 Gigabyte OC", 220.0)])

    alert_loop.run_once()
    assert tg.errors == []


# --------------------------------------------------------- persistent-host mode
def test_single_pass_still_exits_non_zero_on_failure(monkeypatch):
    """GitHub Actions decides whether the chain is healthy from the exit code,
    so swallowing a failure in single-pass mode would turn a broken deploy into
    a green build that alerts nobody."""
    monkeypatch.setattr(sys, "argv", ["alert_loop.py"])
    monkeypatch.setattr(alert_loop, "run_once", lambda: (_ for _ in ()).throw(RuntimeError("boom")))

    with pytest.raises(RuntimeError):
        alert_loop.main()


def test_loop_mode_survives_a_failed_pass_and_backs_off(monkeypatch):
    """Latency is most of the edge here, and --loop nearly halves time-to-alert
    versus the ~5-minute Actions chain — but `while True` had no `try`, so one
    transient Supabase blip stopped a persistent host permanently and silently.
    """
    monkeypatch.setattr(sys, "argv", ["alert_loop.py", "--loop"])
    slept = []
    monkeypatch.setattr(alert_loop.time, "sleep", slept.append)

    calls = {"n": 0}
    good = {"items_seen": 1, "alerts_sent": 0, "junk": 0, "over_cap": 0, "errors": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("supabase blip")
        if calls["n"] == 3:
            return good
        raise KeyboardInterrupt  # the test's way out of `while True`

    monkeypatch.setattr(alert_loop, "run_once", flaky)
    with pytest.raises(KeyboardInterrupt):
        alert_loop.main()

    assert calls["n"] == 4, "a failed pass must not end the process"
    # Two failures back off (45s then 90s), then a good pass resets to the
    # normal cadence — no tight crash-loop hammering Wallapop and Telegram.
    assert slept[0] == config.LOOP_INTERVAL_SECONDS
    assert slept[1] == config.LOOP_INTERVAL_SECONDS * 2
    assert slept[2] == config.LOOP_INTERVAL_SECONDS


def test_loop_backoff_is_capped(monkeypatch):
    """A hard outage — expired credentials, an API shape change — must not back
    off toward infinity either; the ceiling keeps the host checking in."""
    monkeypatch.setattr(sys, "argv", ["alert_loop.py", "--loop"])
    slept = []
    monkeypatch.setattr(alert_loop.time, "sleep", slept.append)

    calls = {"n": 0}

    def always_fails():
        calls["n"] += 1
        if calls["n"] > 20:
            raise KeyboardInterrupt
        raise RuntimeError("credentials expired")

    monkeypatch.setattr(alert_loop, "run_once", always_fails)
    with pytest.raises(KeyboardInterrupt):
        alert_loop.main()

    assert max(slept) == alert_loop.LOOP_BACKOFF_CAP_SECONDS


# --------------------------------------------------------------- seeded searches
def test_every_seeded_search_sends_a_price_floor():
    """Free page depth that was being left unclaimed. No seeded search set
    min_price, so `min_sale_price` never travelled and each 40-item page arrived
    with sub-50 EUR listings that pricing.sane() then discarded locally — after
    they had already spent their slot. The entire time_filter investigation in
    config.py exists to get a page from 16 items to 40; giving slots away for
    listings neither loop can use undoes part of that.
    """
    rows = seed.build_search_rows()
    assert rows
    assert {r["role"] for r in rows} == {"alert", "comps"}
    for row in rows:
        assert row["min_price"] == config.MIN_SANE_PRICE, row["label"]


# ------------------------------------------------------- comps-only families
def test_a_phone_is_never_alerted_however_good_the_margin(wire):
    """The owner asked for iPhone prices to be *tracked*, not traded. Nothing in
    the margin maths knows that, so alert_loop.ALERTING_FAMILIES enforces it.

    This is deliberately tested through a hand-built alert search targeting a
    phone — a row seed.py never creates — because that is the failure this
    guard exists for. Relying on "no phone alert search is seeded" would make
    the protection a property of a data file that anyone can edit from the
    dashboard, rather than of the code.
    """
    search = dict(ALERT_SEARCH, label="iPhone 15 Pro", keywords="iphone 15 pro",
                  model_key="iphone_15_pro", max_price=None)
    prices = {"iphone_15_pro": {"ref_price": 530.0, "buy_ceiling": 400.0,
                                "buy_ceiling_in_person": 450.0, "n_comps": 20,
                                "is_seed": False}}
    db = FakeDB([search], prices)
    tg = wire(alert_loop, db, [make_item("p1", "iPhone 15 Pro 256GB", 120.0)])

    stats = alert_loop.run_once()
    assert stats["alerts_sent"] == 0
    assert stats["non_alerting_family"] == 1
    assert tg.sent == []


def test_a_phone_is_still_observed_so_its_comps_are_collected(wire):
    """Not alerting must not mean not recording: the whole point of tracking a
    family without alerting on it is to accumulate the price history."""
    search = dict(ALERT_SEARCH, label="iPhone 15 Pro", keywords="iphone 15 pro",
                  model_key="iphone_15_pro", max_price=None)
    db = FakeDB([search], {})
    wire(alert_loop, db, [make_item("p2", "iPhone 15 Pro 256GB", 480.0)])

    alert_loop.run_once()
    row = next(r for r in db.listings if r["item_id"] == "p2")
    assert row["model_key"] == "iphone_15_pro"
    assert row["family"] == "phone"
    assert row["storage"] == "256gb"


def test_a_gpu_still_alerts_with_the_family_guard_in_place():
    """Guards the two tests above from passing because the guard rejects
    everything."""
    assert "gpu" in alert_loop.ALERTING_FAMILIES
    assert "phone" not in alert_loop.ALERTING_FAMILIES


def test_a_low_confidence_match_cannot_alert_through_the_bootstrap_cap(wire):
    """The hole the description-confidence cap left open on the other path.

    An unpriceable match never gets a model_row, so pricing.evaluate skips the
    margin engine entirely and falls through to the search's flat bootstrap cap
    — which the listing passes on price alone. Two of these reached the live
    feed in the three days after the description fix landed:

        "Placa gráfica Palit RTX 2070 Super"  -> rtx_3060_ti at 185 EUR,
             because its description reads "Tiene el rendimiento de RTX3060TI"
        "Aoostar AG02 eGPU Adaptador"         -> rtx_4060 at 125 EUR

    Neither is the card it matched. Both merely name one.
    """
    search = dict(ALERT_SEARCH, label="RTX 3060 Ti", keywords="rtx 3060 ti",
                  model_key="rtx_3060_ti", max_price=185)
    db = FakeDB([search], {})
    tg = wire(alert_loop, db, [make_item(
        "lc1", "Placa gráfica Palit RTX 2070 Super PCIx8", 185.0,
        description="Placa grafica Palit RTX 2070 Super. Tiene el rendimiento de RTX3060TI 8GB.",
    )])

    assert alert_loop.run_once()["alerts_sent"] == 0
    assert tg.sent == []


def test_a_confident_match_with_no_comps_still_uses_the_bootstrap_cap(wire):
    """Guards the fix from over-reaching. Confidence and having-a-reference-
    price are different questions: a model classified from its own title, with
    no comps collected yet, is exactly what the bootstrap path exists for."""
    search = dict(ALERT_SEARCH, label="RTX 3060 Ti", keywords="rtx 3060 ti",
                  model_key="rtx_3060_ti", max_price=185)
    db = FakeDB([search], {})
    tg = wire(alert_loop, db, [make_item("hc1", "RTX 3060 Ti Gigabyte OC 8GB", 170.0)])

    assert alert_loop.run_once()["alerts_sent"] == 1
    assert len(tg.sent) == 1


# ------------------------------------------------ comps crash-ping cooldown
# A crash used to message Telegram on every run. When
# get_open_listings_for_models started timing out on roughly every other pass
# in early October 2026, that was an identical traceback every hour — the same
# failure mode the dead-man switch already had a cooldown for, on a path that
# did not. These pin the cooldown rather than the query fix, because the
# reporting shape was wrong independently of that bug.
class _CrashTg:
    def __init__(self, broken=False):
        self.sent = []
        self.broken = broken

    def send_error(self, text):
        if self.broken:
            raise RuntimeError("telegram down")
        self.sent.append(text)


class _CrashDB:
    def __init__(self, runs=(), broken=False):
        self._runs = list(runs)
        self.broken = broken

    def recent_runs(self, loop_name, limit):
        if self.broken:
            raise RuntimeError("db down")
        return self._runs


def _crash_marked_run(hours_ago):
    stamp = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
    return {
        "notes": f"closed=0 repriced=0 {comps_loop.CRASH_MARKER}",
        "started_at": stamp.isoformat(),
    }


def test_first_crash_pings_and_records_the_marker():
    tg = _CrashTg()
    notes = comps_loop._report_crash(_CrashDB(), tg, "boom")
    assert len(tg.sent) == 1
    assert comps_loop.CRASH_MARKER in notes


def test_repeat_crash_inside_cooldown_stays_quiet():
    """The whole point: an hourly crash reports once, not twelve times a day."""
    tg = _CrashTg()
    db = _CrashDB([_crash_marked_run(1)])
    notes = comps_loop._report_crash(db, tg, "boom")
    assert tg.sent == []
    assert comps_loop.CRASH_MARKER in notes, "must stay marked, or the cooldown resets"


def test_crash_pings_again_once_the_cooldown_expires():
    tg = _CrashTg()
    db = _CrashDB([_crash_marked_run(comps_loop.CRASH_COOLDOWN_HOURS + 1)])
    assert comps_loop._report_crash(db, tg, "boom") == comps_loop.CRASH_MARKER
    assert len(tg.sent) == 1


def test_a_new_crash_after_healthy_runs_is_never_suppressed():
    """A fresh fault must not hide behind an older one's cooldown."""
    tg = _CrashTg()
    healthy = {"notes": "closed=12 repriced=5", "started_at": datetime.now(timezone.utc).isoformat()}
    assert comps_loop._report_crash(_CrashDB([healthy]), tg, "boom")
    assert len(tg.sent) == 1


def test_an_undelivered_ping_does_not_open_a_cooldown():
    """Marking on a failed send would buy six hours of silence for a message
    nobody ever received."""
    notes = comps_loop._report_crash(_CrashDB(), _CrashTg(broken=True), "boom")
    assert notes == ""


def test_unreadable_run_history_still_reports_the_crash():
    """The suppression lookup must never be the reason a crash goes unseen."""
    tg = _CrashTg()
    comps_loop._report_crash(_CrashDB(broken=True), tg, "boom")
    assert len(tg.sent) == 1
