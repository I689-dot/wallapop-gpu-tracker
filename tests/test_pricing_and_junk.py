"""Margin maths, comps trimming, and the phrase-only junk filter."""

import statistics
import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config  # noqa: E402
import junk  # noqa: E402
import pricing  # noqa: E402


# ----------------------------------------------------- weighted-comps helpers
class _FakeDB:
    """Minimal stand-in for db.Database, scoped to what pricing.py reads:
    reserved_comps / sold_comps / get_model_prices / upsert_model_price.
    """

    def __init__(self, reserved=None, sold=None, model_prices=None):
        self._reserved = reserved or []
        self._sold = sold or []
        self._model_prices = dict(model_prices or {})
        self.writes = []

    def reserved_comps(self, model_key, since):
        return self._reserved

    def sold_comps(self, model_key, since):
        return self._sold

    def get_model_prices(self):
        return self._model_prices

    def upsert_model_price(self, row):
        self.writes.append(row)
        self._model_prices[row["model_key"]] = row


def _reserved_row(item_id, price, age_days=0.0):
    seen_at = (pricing.now() - timedelta(days=age_days)).isoformat()
    return {"item_id": item_id, "price": price, "seen_at": seen_at}


def _sold_row(item_id, price, age_days=0.0):
    closed_at = (pricing.now() - timedelta(days=age_days)).isoformat()
    return {"item_id": item_id, "sold_price": price, "closed_at": closed_at}


class _ObservationsDB(_FakeDB):
    """Fake holding a raw `observations` table, active rows included.

    _FakeDB hands back whatever list it was constructed with, which cannot prove
    the "no asking prices in the pool" invariant: the status filter lives in
    db.reserved_comps, so a fake that never sees an active row proves nothing
    about what happens when one exists. This one stores the table and applies the
    same predicate the real query does (status == 'reserved', inside the window,
    newest first), so an active observation is genuinely offered to
    collect_comps' input and genuinely has to be rejected.
    """

    def __init__(self, observations=None, **kwargs):
        super().__init__(**kwargs)
        self._observations = list(observations or [])

    def reserved_comps(self, model_key, since):
        rows = [
            row
            for row in self._observations
            if row.get("status") == "reserved"
            and pricing._parse_dt(row["seen_at"]) >= since
        ]
        return sorted(rows, key=lambda row: row["seen_at"], reverse=True)


def _observation(item_id, price, status, age_days=0.0):
    seen_at = (pricing.now() - timedelta(days=age_days)).isoformat()
    return {"item_id": item_id, "price": price, "status": status, "seen_at": seen_at}


# ------------------------------------------------------------------ margins
def test_flat_floor_governs_cheap_items(monkeypatch):
    """Below the TARGET_MARGIN / MARGIN_RATE crossover (~278 EUR at the
    defaults) the flat 50 EUR floor still decides, exactly as it always did."""
    fees = config.FeeModel(0.10, 0.075, 0.69, 4.50, 50.0, "shipped")
    assert fees.required_margin(200) == pytest.approx(50.0)
    assert fees.buy_ceiling(200) == pytest.approx((200 * 0.9 - 0.69 - 4.5 - 50) / 1.075)


def test_rate_governs_expensive_items():
    """50 EUR on a 620 EUR card is an 8% return for the same work and risk as a
    25% one on a 200 EUR card, so above the crossover the percentage binds."""
    fees = config.FeeModel(0.10, 0.075, 0.69, 4.50, 50.0, "shipped")
    assert fees.required_margin(620) == pytest.approx(0.18 * 620)
    assert fees.required_margin(330) == pytest.approx(59.4)


def test_in_person_ceiling_is_ref_minus_required_margin():
    fees = config.FeeModel(0.0, 0.0, 0.0, 0.0, 50.0, "in person")
    assert fees.buy_ceiling(200) == pytest.approx(150.0)          # flat floor
    assert fees.buy_ceiling(330) == pytest.approx(330 - 59.4)     # rate binds


def test_buying_at_the_ceiling_nets_exactly_the_required_margin():
    fees = config.FeeModel(0.10, 0.075, 0.69, 4.50, 50.0, "shipped")
    for ref in (200, 330, 620):
        ceiling = fees.buy_ceiling(ref)
        assert fees.net_margin(ceiling, ref) == pytest.approx(fees.required_margin(ref))


def test_margin_rate_can_be_disabled(monkeypatch):
    """MARGIN_RATE=0 restores the old flat-only behaviour verbatim."""
    monkeypatch.setattr(config, "MARGIN_RATE", 0.0)
    fees = config.FeeModel(0.10, 0.075, 0.69, 4.50, 50.0, "shipped")
    assert fees.required_margin(620) == pytest.approx(50.0)
    assert fees.buy_ceiling(330) == pytest.approx(224.0, abs=1.0)


# -------------------------------------------------------------------- comps
def test_trimmed_median_drops_outliers():
    # One typo price and one bundle must not move the answer.
    prices = [30.0, 300, 310, 315, 320, 325, 330, 335, 340, 1500.0]
    assert pricing.trimmed_median(prices, trim=0.10) == pytest.approx(322.5)


def test_trim_is_skipped_on_tiny_samples():
    assert pricing.trimmed_median([100, 200, 300], trim=0.10) == 200


def test_trimmed_median_of_empty_is_none():
    assert pricing.trimmed_median([]) is None


def test_sanity_band():
    assert not pricing.sane(5)
    assert not pricing.sane(99999)
    assert not pricing.sane(None)
    assert pricing.sane(300)


def test_sub_50_eur_gpu_is_never_a_real_deal():
    """A real GPU under 50 EUR is broken, fake, or bait — never a genuine
    flip, so it's rejected outright rather than treated as a great margin."""
    assert not pricing.sane(49.99)
    assert not pricing.sane(30)
    assert pricing.sane(50)


# ------------------------------------------------------- trim dead-zone fix
@pytest.mark.parametrize("n", range(5, 10))
def test_trim_dead_zone_fixed_for_min_comps_regime(n):
    """Old formula: k = int(n * TRIM_FRACTION) is 0 for every n in [5, 9] —
    exactly MIN_COMPS through roughly double it, the size a freshly-priced
    model sits at for a while — so a sample in this range got zero outlier
    protection. round() plus a floor of 1 must engage here instead.
    """
    assert int(n * config.TRIM_FRACTION) == 0            # the bug, confirmed still true of the naive formula
    assert pricing._trim_k(n, config.TRIM_FRACTION) >= 1  # the fix


def test_trim_k_still_respects_the_tiny_sample_floor():
    # n=3: dropping k=1 from each end would leave only 1 item, under the
    # n-2k>=3 safety guard, so trimming must not fire — unchanged by the fix.
    assert pricing.trimmed_median([100.0, 200.0, 300.0], trim=0.10) == 200


def test_trim_is_a_no_op_when_trim_fraction_is_zero():
    assert pricing._trim_k(7, 0.0) == 0


def test_trimmed_median_is_provably_invariant_to_symmetric_trim():
    """Why the fix above doesn't change trimmed_median()'s *output* at
    n=5..9: dropping k items off each end of a sorted list never moves which
    order statistic(s) the plain median falls on, so trimmed_median() equals
    the untrimmed statistics.median() any time the guard lets it trim at
    all — trimming-then-taking-a-median is a no-op by construction. The
    bug fix earns its keep on the *weighted* pool instead (see
    test_weighted_trim_kills_a_heavily_weighted_outlier_at_n7 below), where
    a heavily-weighted outlier's removal genuinely shifts the interpolated
    quantile that survives it.
    """
    values = [30.0, 300.0, 310.0, 315.0, 320.0]  # n=5, one bad typo price
    assert pricing.trimmed_median(values, trim=0.10) == pricing.trimmed_median(values, trim=0.0)


def test_weighted_trim_kills_a_heavily_weighted_outlier_at_n7():
    """A heavily-weighted (e.g. very recent) but obviously wrong low price
    would otherwise anchor the low end of the weighted interpolation and
    drag the whole quantile down with it — the "single outlier does the
    most damage in a small sample" scenario the dead-zone bug left
    unprotected. n=7 sits squarely in the [5, 9] regime the fix targets.
    """
    pairs = [
        (10.0, 10.0), (300.0, 1.0), (305.0, 1.0), (310.0, 1.0),
        (315.0, 1.0), (320.0, 1.0), (2000.0, 1.0),
    ]
    untrimmed = pricing.weighted_quantile(pairs, 0.5)
    trimmed = pricing.weighted_quantile(pricing._trim_pairs(pairs, config.TRIM_FRACTION), 0.5)
    assert untrimmed == pytest.approx(168.18, abs=0.01)  # the outlier drags it way down
    assert trimmed == pytest.approx(310.0)               # trimmed, it lands on the real cluster


# --------------------------------------------------------- weighted_quantile
def test_weighted_quantile_hand_computed():
    # Sorted: (10, w1), (20, w1), (30, w2); total weight 4. Midpoint
    # positions (cumulative weight so far minus half its own, over total):
    #   10 -> (1 - 0.5)/4 = 0.125
    #   20 -> (2 - 0.5)/4 = 0.375
    #   30 -> (4 - 1.0)/4 = 0.75
    # q=0.5 sits 1/3 of the way from the 20-slot to the 30-slot:
    #   20 + 1/3 * (30 - 20) = 23.333...
    pairs = [(30.0, 2.0), (10.0, 1.0), (20.0, 1.0)]
    assert pricing.weighted_quantile(pairs, 0.5) == pytest.approx(23.3333, abs=1e-3)


def test_weighted_quantile_matches_plain_median_at_equal_weights():
    values = [10.0, 40.0, 30.0, 20.0, 50.0]
    pairs = [(v, 1.0) for v in values]
    assert pricing.weighted_quantile(pairs, 0.5) == pytest.approx(statistics.median(values))


def test_weighted_quantile_extremes_and_empty():
    pairs = [(10.0, 1.0), (20.0, 3.0), (30.0, 1.0)]
    assert pricing.weighted_quantile(pairs, 0.0) == 10.0
    assert pricing.weighted_quantile(pairs, 1.0) == 30.0
    assert pricing.weighted_quantile([], 0.5) is None


# ------------------------------------------------------------- time decay
def test_time_decay_halves_at_the_configured_halflife():
    assert pricing._time_decay(0) == pytest.approx(1.0)
    assert pricing._time_decay(config.COMPS_HALFLIFE_DAYS) == pytest.approx(0.5)
    assert pricing._time_decay(2 * config.COMPS_HALFLIFE_DAYS) == pytest.approx(0.25)


def test_time_decay_is_monotonically_decreasing_with_age():
    weights = [pricing._time_decay(a) for a in (0, 5, 14, 30, 60)]
    assert weights == sorted(weights, reverse=True)
    assert weights[-1] < weights[0]


def test_collect_comps_weights_a_fresh_comp_above_a_stale_one():
    """Same price, same source, only age differs — GPU prices fall
    monotonically, so a 55-day-old comp must count for less than a
    2-day-old one, not the same."""
    db = _FakeDB(reserved=[
        _reserved_row("fresh", 300.0, age_days=2),
        _reserved_row("stale", 300.0, age_days=55),
    ])
    fresh_comp, stale_comp = pricing.collect_comps(db, "rtx_4070")
    assert fresh_comp.weight > stale_comp.weight
    assert fresh_comp.weight == pytest.approx(config.RESERVED_WEIGHT * pricing._time_decay(2), rel=1e-3)
    assert stale_comp.weight == pytest.approx(config.RESERVED_WEIGHT * pricing._time_decay(55), rel=1e-3)


# ------------------------------------------------------ sold vs. reserved
def test_sold_outranks_reserved_at_equal_age():
    """Reservations fall through, so a confirmed sale at the same age must
    outweigh a reservation — SOLD_WEIGHT > RESERVED_WEIGHT."""
    db = _FakeDB(
        reserved=[_reserved_row("r1", 300.0, age_days=5)],
        sold=[_sold_row("s1", 300.0, age_days=5)],
    )
    by_source = {c.source: c for c in pricing.collect_comps(db, "rtx_4070")}
    assert by_source["sold"].weight > by_source["reserved"].weight
    assert by_source["sold"].weight == pytest.approx(config.SOLD_WEIGHT * pricing._time_decay(5), rel=1e-3)
    assert by_source["reserved"].weight == pytest.approx(config.RESERVED_WEIGHT * pricing._time_decay(5), rel=1e-3)


def test_sale_overwrites_reservation_for_the_same_item():
    """Per-item dedup still holds: a card that was reserved then confirmed
    sold contributes once, as its sale."""
    db = _FakeDB(
        reserved=[_reserved_row("x", 250.0, age_days=1)],
        sold=[_sold_row("x", 260.0, age_days=1)],
    )
    comps = pricing.collect_comps(db, "rtx_4070")
    assert len(comps) == 1
    assert comps[0].source == "sold"
    assert comps[0].price == 260.0


def test_reserved_comp_sitting_for_weeks_still_counts_once():
    """500 observations of the same reserved card must not outvote real
    turnover — dedup by item_id, first hit (newest) wins."""
    db = _FakeDB(reserved=[_reserved_row("stuck", 300.0, age_days=d) for d in range(20)])
    comps = pricing.collect_comps(db, "rtx_4070")
    assert len(comps) == 1


# ------------------------------------- only committed prices may become comps
def test_an_active_asking_price_never_becomes_a_comp():
    """The owner's requirement, verbatim: "The reference price must be taken from
    reserved price graphics card ... cause people can ask whatever they want, but
    i want to know the real selling price."

    Three cards sitting at a 999 EUR ask and one actually reserved at 300 must
    produce exactly one comp, worth 300. A pool of asks measures optimism.
    """
    db = _ObservationsDB(observations=[
        _observation("ask1", 999.0, "active", age_days=1),
        _observation("ask2", 950.0, "active", age_days=2),
        _observation("ask3", 900.0, "active", age_days=3),
        _observation("real", 300.0, "reserved", age_days=1),
    ])
    comps = pricing.collect_comps(db, "rtx_4070")
    assert [(c.price, c.source) for c in comps] == [(300.0, "reserved")]


def test_a_model_with_only_active_listings_has_no_comps_at_all():
    """No committed price means no reference price — the model stays on its seed
    and the bootstrap cap, rather than learning a price from asking prices."""
    db = _ObservationsDB(observations=[
        _observation(f"a{i}", 400.0, "active") for i in range(30)
    ])
    assert pricing.collect_comps(db, "rtx_4070") == []
    assert pricing.recompute_model_price(db, "rtx_4070", None) is None
    assert db.writes == []


def test_the_same_item_asking_high_and_reserved_low_contributes_only_the_reservation():
    """A listing whose ask was cut before it reserved must contribute the price
    someone committed to, never the price it was hoping for."""
    db = _ObservationsDB(observations=[
        _observation("x", 500.0, "active", age_days=9),
        _observation("x", 420.0, "active", age_days=5),
        _observation("x", 300.0, "reserved", age_days=1),
    ])
    comps = pricing.collect_comps(db, "rtx_4070")
    assert [c.price for c in comps] == [300.0]


def test_comp_sane_reads_min_comp_price_not_min_sane_price(monkeypatch):
    """The pool floor and the alert floor are separate knobs that only happen to
    be equal at the defaults — moving one must not move the other."""
    monkeypatch.setattr(config, "MIN_COMP_PRICE", 120.0)
    assert not pricing.comp_sane(100.0)
    assert pricing.sane(100.0)
    assert pricing.comp_sane(120.0)
    assert not pricing.comp_sane(None)
    assert not pricing.comp_sane(config.MAX_COMP_PRICE + 1)


def test_the_pool_ceiling_is_separate_from_the_alert_ceiling():
    """The ceilings are separate for the same reason the floors are, and this is
    the case that motivated it: with MAX_SANE_PRICE lowered to 1000, a 1400 EUR
    sale is not something to alert on but is still evidence of what the card is
    worth. Coupling them froze the priciest models on their seed guesses."""
    assert config.MAX_COMP_PRICE > config.MAX_SANE_PRICE
    dear = config.MAX_SANE_PRICE + 400
    assert not pricing.sane(dear)        # never a trade
    assert pricing.comp_sane(dear)       # still a lesson
    assert pricing.ref_sane(dear)        # and it can be stored


def test_a_reference_above_the_alert_ceiling_is_still_learned(monkeypatch):
    """recompute_model_price used to refuse any reference over MAX_SANE_PRICE,
    so a model whose comps all sat above the alert ceiling could never record
    what it had learned."""
    monkeypatch.setattr(pricing, "_time_decay", lambda age: 1.0)
    reserved = [_reserved_row(f"r{i}", 1400.0) for i in range(12)]
    db = _FakeDB(reserved=reserved)

    row = pricing.recompute_model_price(db, "rtx_5080", {"ref_price": 1400.0, "is_seed": False})

    assert row is not None, "a legitimately expensive model must still learn"
    assert row["ref_price"] > config.MAX_SANE_PRICE


@pytest.mark.parametrize("price,admitted", [
    (4.0, False),      # the literal typo the requirement named
    (40.0, False),     # not a typo: a dead card, a replica or a bare cooler
    (49.99, False),
    (50.0, True),      # MIN_COMP_PRICE itself is inclusive
    (300.0, True),
])
def test_prices_below_min_comp_price_never_enter_the_pool(price, admitted):
    """MIN_COMP_PRICE, not MIN_SANE_PRICE, is the pool's lower bound — and it
    applies to both committed-price branches, reserved and sold alike. A real
    40 EUR GPU drags the median down exactly as hard as a 4 EUR typo does.
    """
    reserved = pricing.collect_comps(_FakeDB(reserved=[_reserved_row("r", price)]), "rtx_4070")
    sold = pricing.collect_comps(_FakeDB(sold=[_sold_row("s", price)]), "rtx_4070")
    assert bool(reserved) is admitted
    assert bool(sold) is admitted


def test_raising_min_comp_price_tightens_the_pool_without_touching_the_alert_path(monkeypatch):
    monkeypatch.setattr(config, "MIN_COMP_PRICE", 200.0)
    db = _FakeDB(
        reserved=[_reserved_row("cheap", 150.0), _reserved_row("ok", 300.0)],
        sold=[_sold_row("cheap-sale", 180.0)],
    )
    assert [c.price for c in pricing.collect_comps(db, "rtx_4070")] == [300.0]
    # ...while the alert path still evaluates a 150 EUR listing as it always did.
    assert pricing.sane(150.0)


# ------------------------------------------------------------- prior lookup
def test_prior_price_prefers_own_existing_ref_price():
    db = _FakeDB(model_prices={"rtx_4060_ti_8g": {"ref_price": 999.0}})
    assert pricing._prior_price(db, "rtx_4060_ti", {"ref_price": 280.0}) == pytest.approx(280.0)


def test_prior_price_falls_back_to_sibling_average_when_no_own_history():
    db = _FakeDB(model_prices={
        "rtx_4060_ti_8g": {"ref_price": 270.0},
        "rtx_4060_ti_16g": {"ref_price": 330.0},
    })
    assert pricing._prior_price(db, "rtx_4060_ti", None) == pytest.approx(300.0)


def test_prior_price_none_when_nothing_available():
    db = _FakeDB()
    assert pricing._prior_price(db, "rtx_4070", None) is None


# ---------------------------------------------------------------- shrinkage
def test_shrinkage_pulls_hard_toward_prior_with_few_comps(monkeypatch):
    """Right at MIN_COMPS, the sample is thin — the reference should land
    noticeably closer to the seed/prior than a naive median-only estimate
    would, replacing the old hard cliff (n=4 pure seed, n=5 pure observed)
    with a smooth blend."""
    monkeypatch.setattr(pricing, "_time_decay", lambda age: 1.0)  # isolate from clock noise
    reserved = [_reserved_row(f"r{i}", 300.0) for i in range(config.MIN_COMPS)]
    db = _FakeDB(reserved=reserved)
    existing = {"ref_price": 500.0, "is_seed": True}

    row = pricing.recompute_model_price(db, "rtx_4070", existing)

    assert row is not None
    assert row["raw_ref"] == pytest.approx(300.0)
    assert row["shrunk"] is True
    # n_eff is the *trimmed* pool's weight — trimming drops k from each end
    # first (k=1 here, per the dead-zone fix), so only 3 of the 5 comps feed it.
    # Mirror _trim_pairs exactly, including its `n - 2k >= 3` safety guard:
    # below that it declines to trim at all. At MIN_COMPS=3 the guard always
    # bites, so a three-comp reference gets NO outlier trimming whatsoever.
    k = pricing._trim_k(config.MIN_COMPS, config.TRIM_FRACTION)
    if not (k and config.MIN_COMPS - 2 * k >= 3):
        k = 0
    n_eff = (config.MIN_COMPS - 2 * k) * config.RESERVED_WEIGHT
    expected = (n_eff * 300.0 + config.PRIOR_WEIGHT * 500.0) / (n_eff + config.PRIOR_WEIGHT)
    assert row["ref_price"] == pytest.approx(round(expected, 2))
    # pulled toward the prior: closer to 500 than to the raw 300 observation
    assert abs(500.0 - row["ref_price"]) < abs(300.0 - row["ref_price"])


def test_shrinkage_barely_moves_the_reference_with_many_comps(monkeypatch):
    """With a healthy sample, the same prior should have little pull — the
    reference should sit close to what was actually observed, not the stale
    seed."""
    monkeypatch.setattr(pricing, "_time_decay", lambda age: 1.0)
    n = 60
    reserved = [_reserved_row(f"r{i}", 300.0) for i in range(n)]
    db = _FakeDB(reserved=reserved)
    existing = {"ref_price": 500.0, "is_seed": True}

    row = pricing.recompute_model_price(db, "rtx_4070", existing)

    assert row is not None
    assert row["raw_ref"] == pytest.approx(300.0)
    k = pricing._trim_k(n, config.TRIM_FRACTION)
    n_eff = (n - 2 * k) * config.RESERVED_WEIGHT
    expected = (n_eff * 300.0 + config.PRIOR_WEIGHT * 500.0) / (n_eff + config.PRIOR_WEIGHT)
    assert row["ref_price"] == pytest.approx(round(expected, 2))
    assert abs(row["ref_price"] - 300.0) < 30.0    # close to the real observation
    assert abs(row["ref_price"] - 500.0) > 150.0   # nowhere near the stale prior


def test_no_prior_available_leaves_ref_price_unshrunk(monkeypatch):
    monkeypatch.setattr(pricing, "_time_decay", lambda age: 1.0)
    reserved = [_reserved_row(f"r{i}", 300.0) for i in range(config.MIN_COMPS)]
    db = _FakeDB(reserved=reserved)  # no existing row, no siblings for a leaf SKU

    row = pricing.recompute_model_price(db, "rtx_4070", None)

    assert row is not None
    assert row["shrunk"] is False
    assert row["ref_price"] == row["raw_ref"] == pytest.approx(300.0)


# --------------------------------------------------------------- provenance
def test_recompute_model_price_records_provenance(monkeypatch):
    monkeypatch.setattr(pricing, "_time_decay", lambda age: 1.0)
    reserved = [_reserved_row(f"r{i}", 300.0) for i in range(4)]
    sold = [_sold_row(f"s{i}", 305.0) for i in range(3)]
    db = _FakeDB(reserved=reserved, sold=sold)

    row = pricing.recompute_model_price(db, "rtx_4070", None)

    assert row is not None
    assert row["n_sold"] == 3
    assert row["n_reserved"] == 4
    assert row["n_comps"] == 7
    assert row["raw_ref"] is not None
    assert row["shrunk"] is False  # no existing ref_price and no sibling prior for a leaf SKU


def test_recompute_model_price_below_min_comps_writes_nothing():
    db = _FakeDB(reserved=[_reserved_row("only-one", 300.0)])
    assert pricing.recompute_model_price(db, "rtx_4070", None) is None
    assert db.writes == []


# -------------------------------------------------- borrowed comps aren't "own"
class _PerModelDB(_FakeDB):
    """_FakeDB with per-model reserved/sold tables.

    The shared-table fake returns the same rows for every model_key, which makes
    a borrowed sibling comp indistinguishable from an owned one — precisely the
    distinction n_own exists to record.
    """

    def __init__(self, reserved_by_model=None, sold_by_model=None, model_prices=None):
        super().__init__(model_prices=model_prices)
        self._reserved_by_model = reserved_by_model or {}
        self._sold_by_model = sold_by_model or {}

    def reserved_comps(self, model_key, since):
        return list(self._reserved_by_model.get(model_key, []))

    def sold_comps(self, model_key, since):
        return list(self._sold_by_model.get(model_key, []))


def test_n_own_reports_zero_when_the_whole_pool_was_borrowed(monkeypatch):
    """A generic key with no comps of its own used to advertise the combined
    sibling pool as its own evidence — "n=8" on a model that has observed
    nothing. That number is printed as provenance in the alert and
    drives the dashboard's confidence badge, so both overstated what is known.
    n_comps stays the total the price was computed from; n_own is the honest one.
    """
    monkeypatch.setattr(pricing, "_time_decay", lambda age: 1.0)
    db = _PerModelDB(reserved_by_model={
        "rtx_4060_ti_8g": [_reserved_row(f"a{i}", 270.0) for i in range(4)],
        "rtx_4060_ti_16g": [_reserved_row(f"b{i}", 330.0) for i in range(4)],
    })

    row = pricing.recompute_model_price(db, "rtx_4060_ti", None)

    assert row is not None
    assert row["n_comps"] == 8
    assert row["n_own"] == 0


def test_n_own_counts_only_this_models_comps_when_borrowing_tops_up(monkeypatch):
    monkeypatch.setattr(pricing, "_time_decay", lambda age: 1.0)
    db = _PerModelDB(reserved_by_model={
        "rtx_4060_ti": [_reserved_row("own1", 300.0), _reserved_row("own2", 305.0)],
        "rtx_4060_ti_8g": [_reserved_row(f"a{i}", 270.0) for i in range(3)],
        "rtx_4060_ti_16g": [_reserved_row(f"b{i}", 330.0) for i in range(3)],
    })

    row = pricing.recompute_model_price(db, "rtx_4060_ti", None)

    assert row["n_own"] == 2
    assert row["n_comps"] == 8


def test_n_own_equals_n_comps_when_nothing_was_borrowed(monkeypatch):
    monkeypatch.setattr(pricing, "_time_decay", lambda age: 1.0)
    db = _FakeDB(reserved=[_reserved_row(f"r{i}", 300.0) for i in range(config.MIN_COMPS)])
    row = pricing.recompute_model_price(db, "rtx_4070", None)
    assert row["n_own"] == row["n_comps"] == config.MIN_COMPS


# ------------------------------------------- is_seed tracks the written number
def test_first_recompute_off_a_thin_decayed_pool_stays_seeded():
    """BUG THIS PINS: is_seed was hardcoded False on the first successful
    recompute, but the value written is shrunk toward the prior and on a first
    run the prior *is* the seed. A month-old pool of five reserved comps carries
    an n_eff of well under 1 against PRIOR_WEIGHT=5, so the seed is the
    overwhelming majority of ref_price — and SEED_MARGIN_MULTIPLIER, the only
    defence against a bad hand-written guess, was switched off right there.

    Deliberately does *not* pin _time_decay: the decay is the point.
    """
    reserved = [_reserved_row(f"r{i}", 300.0, age_days=30) for i in range(config.MIN_COMPS)]
    db = _FakeDB(reserved=reserved)
    existing = {"ref_price": 500.0, "is_seed": True}

    row = pricing.recompute_model_price(db, "rtx_4070", existing)

    assert row is not None
    assert row["is_seed"] is True
    # ...and the flag is telling the truth: the output is still mostly the seed.
    assert abs(row["ref_price"] - 500.0) < abs(row["ref_price"] - 300.0)


def test_a_fat_recent_pool_clears_the_seed_flag(monkeypatch):
    monkeypatch.setattr(pricing, "_time_decay", lambda age: 1.0)
    reserved = [_reserved_row(f"r{i}", 300.0) for i in range(60)]
    db = _FakeDB(reserved=reserved)
    existing = {"ref_price": 500.0, "is_seed": True}

    row = pricing.recompute_model_price(db, "rtx_4070", existing)

    assert row["is_seed"] is False
    assert abs(row["ref_price"] - 300.0) < abs(row["ref_price"] - 500.0)


@pytest.mark.parametrize("n_sold,expect_seed", [(7, True), (8, False)])
def test_the_seed_flag_releases_only_once_evidence_outweighs_the_prior(
    monkeypatch, n_sold, expect_seed
):
    """The comparison is n_eff <= PRIOR_WEIGHT, and the boundary is deliberate.
    The prior's share of the blend is PRIOR_WEIGHT / (n_eff + PRIOR_WEIGHT), so
    at n_eff == PRIOR_WEIGHT the answer is still exactly half seed — not earned.

    Arithmetic, with decay pinned to 1 and SOLD_WEIGHT=1: trimming drops k=1 from
    each end, so 7 sold comps leave 5 (n_eff = 5.0, exactly PRIOR_WEIGHT, still
    seeded) and 8 leave 6 (n_eff = 6.0, released).
    """
    monkeypatch.setattr(pricing, "_time_decay", lambda age: 1.0)
    monkeypatch.setattr(config, "SOLD_WEIGHT", 1.0)
    monkeypatch.setattr(config, "PRIOR_WEIGHT", 5.0)
    db = _FakeDB(sold=[_sold_row(f"s{i}", 300.0 + i) for i in range(n_sold)])

    row = pricing.recompute_model_price(db, "rtx_4070", {"ref_price": 500.0, "is_seed": True})
    assert row["is_seed"] is expect_seed


def test_a_purely_observed_reference_is_never_flagged_as_a_seed():
    """No prior at all means ref_price is 100% observed — there is no guess to
    demand extra margin against, however thin the pool is."""
    reserved = [_reserved_row(f"r{i}", 300.0, age_days=40) for i in range(config.MIN_COMPS)]
    db = _FakeDB(reserved=reserved)  # leaf SKU: no existing row, no siblings

    row = pricing.recompute_model_price(db, "rtx_4070", None)

    assert row["shrunk"] is False
    assert row["is_seed"] is False


def test_the_seed_flag_does_not_come_back_once_a_price_has_been_learned():
    """A one-way ratchet on purpose. Once real comps outweighed the seed the
    stored price is evidence, so shrinking toward it later is shrinking toward
    evidence — re-imposing the seed penalty would be punishing a number nobody
    hand-wrote."""
    reserved = [_reserved_row(f"r{i}", 300.0, age_days=30) for i in range(config.MIN_COMPS)]
    db = _FakeDB(reserved=reserved)
    learned = {"ref_price": 310.0, "is_seed": False}

    row = pricing.recompute_model_price(db, "rtx_4070", learned)

    assert row["shrunk"] is True
    assert row["is_seed"] is False


# ----------------------------------------------------------- time to sale
def test_time_to_sale_days_hand_computed():
    now = pricing.now()
    rows = [
        {"closed_at": now.isoformat(), "posted_at": (now - timedelta(days=d)).isoformat()}
        for d in (2, 3, 4, 5, 6)
    ]
    assert pricing.time_to_sale_days(rows) == pytest.approx(4.0, abs=0.01)  # median of 2,3,4,5,6


def test_time_to_sale_days_falls_back_to_first_seen():
    now = pricing.now()
    rows = [
        {"closed_at": now.isoformat(), "first_seen": (now - timedelta(days=d)).isoformat()}
        for d in (1, 2, 3, 4, 5)
    ]
    assert pricing.time_to_sale_days(rows) == pytest.approx(3.0, abs=0.01)


def test_time_to_sale_days_none_when_not_enough_data():
    now = pricing.now()
    rows = [
        {"closed_at": now.isoformat(), "posted_at": (now - timedelta(days=3)).isoformat()}
        for _ in range(config.MIN_COMPS - 1)
    ]
    assert pricing.time_to_sale_days(rows) is None


def test_time_to_sale_days_ignores_rows_missing_timestamps():
    now = pricing.now()
    rows = [
        {"closed_at": now.isoformat(), "posted_at": (now - timedelta(days=d)).isoformat()}
        for d in (1, 2, 3, 4, 5)
    ]
    rows.append({"closed_at": None, "posted_at": now.isoformat()})  # unusable, must not crash or count
    assert pricing.time_to_sale_days(rows) == pytest.approx(3.0, abs=0.01)


def test_recompute_model_price_uses_supplied_sold_rows_for_time_to_sale(monkeypatch):
    monkeypatch.setattr(pricing, "_time_decay", lambda age: 1.0)
    reserved = [_reserved_row(f"r{i}", 300.0) for i in range(config.MIN_COMPS)]
    db = _FakeDB(reserved=reserved)
    now = pricing.now()
    sold_rows = [
        {"closed_at": now.isoformat(), "posted_at": (now - timedelta(days=d)).isoformat()}
        for d in (2, 3, 4, 5, 6)
    ]

    row = pricing.recompute_model_price(db, "rtx_4070", None, sold_rows=sold_rows)
    assert row["median_days_to_sale"] == pytest.approx(4.0, abs=0.01)


def test_recompute_model_price_carries_forward_median_days_to_sale_when_not_supplied(monkeypatch):
    monkeypatch.setattr(pricing, "_time_decay", lambda age: 1.0)
    reserved = [_reserved_row(f"r{i}", 300.0) for i in range(config.MIN_COMPS)]
    db = _FakeDB(reserved=reserved)
    existing = {"ref_price": 300.0, "median_days_to_sale": 12.5}

    row = pricing.recompute_model_price(db, "rtx_4070", existing)
    assert row["median_days_to_sale"] == pytest.approx(12.5)


# ------------------------------------------------------------------- gating
def test_evaluate_gates_on_the_offer_price_not_the_asking_price(monkeypatch):
    """qualifies iff a haggled offer (asking * (1 - OFFER_DISCOUNT)) clears the
    learned buy_ceiling — not the raw asking price. ceiling=224, discount=20%
    means any asking price up to 224/0.8=280 should qualify.
    """
    monkeypatch.setattr(config, "OFFER_DISCOUNT", 0.20)
    row = {"ref_price": 330, "buy_ceiling": 224.0, "buy_ceiling_in_person": 280.0, "n_comps": 12}
    # 220, not 210: MIN_PLAUSIBLE_RATIO is 0.65 now, so 0.65*330 = 214.50 is the
    # floor and a 210 asking price is rejected before the offer gate is reached.
    assert pricing.evaluate(220, row, None).qualifies       # offer 176 <= 224
    assert pricing.evaluate(250, row, None).qualifies       # offer 200 <= 224 — rescued by haggling
    assert not pricing.evaluate(290, row, None).qualifies   # offer 232 > 224 — still too rich


def test_offer_discount_rescues_a_listing_that_fails_at_asking_price(monkeypatch):
    """The whole point of the offer-based gate: even when the asking price
    alone wouldn't have cleared the old ceiling check, the listing still
    qualifies if a plausible 20% haggle would get there — you can always
    propose the discount to the seller and see if it lands.
    """
    monkeypatch.setattr(config, "OFFER_DISCOUNT", 0.20)
    row = {"ref_price": 450, "buy_ceiling": 367.27, "buy_ceiling_in_person": 400.0, "n_comps": 20}
    deal = pricing.evaluate(400, row, None)  # 400 > 367.27 asking, but offer 320 <= 367.27
    assert deal.qualifies
    assert deal.offer_price == pytest.approx(320.0)


def test_evaluate_reports_net_margin_at_offer_and_at_asking(monkeypatch):
    # Pin the fee model so a local .env override can't change the expectation.
    monkeypatch.setattr(config, "SHIPPED", config.FeeModel(0.0, 0.075, 0.69, 4.50, 50.0, "s"))
    monkeypatch.setattr(config, "OFFER_DISCOUNT", 0.20)
    row = {"ref_price": 330, "buy_ceiling": 224.0, "buy_ceiling_in_person": 280.0, "n_comps": 12}
    deal = pricing.evaluate(280, row, None)  # asking 280, offer = 280*0.8 = 224
    assert deal.offer_price == pytest.approx(224.0)
    # net at offer = 330 - (224*1.075 + 0.69 + 4.50) = 84.01
    assert deal.net_shipped == pytest.approx(84.01, abs=0.05)
    # net at asking = 330 - (280*1.075 + 0.69 + 4.50) = 23.81 (below the 50 target the old gate required)
    assert deal.net_shipped_at_asking == pytest.approx(23.81, abs=0.05)
    # in-person has zero fees either way: ref - buy
    assert deal.net_in_person == pytest.approx(106.0, abs=0.05)


def test_evaluate_falls_back_to_bootstrap_cap():
    """Without a learned reference, there's no margin to haggle against — the
    bootstrap path stays a plain asking-price cap."""
    assert pricing.evaluate(180, None, 200).qualifies
    assert not pricing.evaluate(220, None, 200).qualifies


def test_evaluate_without_reference_or_cap_never_fires():
    assert not pricing.evaluate(180, None, None).qualifies


def test_evaluate_passes_through_provenance_fields():
    """The alert needs to be able to show whether a reference rests on real
    sales or just reservations, and how long the model typically takes to
    move — evaluate() must carry those straight through from model_row."""
    row = {
        "ref_price": 330, "buy_ceiling": 224.0, "buy_ceiling_in_person": 280.0,
        "n_comps": 12, "n_sold": 8, "n_reserved": 4, "median_days_to_sale": 9.5,
    }
    deal = pricing.evaluate(250, row, None)
    assert deal.n_sold == 8
    assert deal.n_reserved == 4
    assert deal.median_days_to_sale == pytest.approx(9.5)


def test_evaluate_defaults_provenance_fields_when_absent():
    """Older/seed rows won't have the new columns populated yet — evaluate()
    must not blow up, and should default sensibly."""
    row = {"ref_price": 330, "buy_ceiling": 224.0, "buy_ceiling_in_person": 280.0, "n_comps": 12}
    deal = pricing.evaluate(250, row, None)
    assert deal.n_sold == 0
    assert deal.n_reserved == 0
    assert deal.median_days_to_sale is None


# --------------------------------------------------------------------- junk
@pytest.mark.parametrize(
    "title",
    [
        "RTX 3070 no funciona, para piezas",
        "Gráfica RTX 4070 NO ENCIENDE",
        "RTX 3080 para reparar",
        "Solo la caja de una RTX 4090",
        "Busco RTX 4070 barata",
        "COMPRO grafica rtx",
        "Waterblock para RTX 3080",
    ],
)
def test_junk_is_excluded(title):
    assert junk.check(title).excluded


@pytest.mark.parametrize(
    "title",
    [
        "RTX 3070 perfecto estado, no acepto cambios",
        "RTX 4070 Ti, no busco cambios, solo venta",
        "RTX 4060 funciona perfectamente",
        "RTX 3080 con caja y accesorios",
        "RX 7800 XT probada en varios juegos",
    ],
)
def test_good_listings_survive(title):
    verdict = junk.check(title)
    assert not verdict.excluded, f"wrongly excluded on {verdict.phrase!r}"


def test_wanted_words_only_count_at_the_start():
    assert junk.check("Busco RTX 4070").excluded
    assert not junk.check("RTX 4070 barata, busco comprador serio").excluded


@pytest.mark.parametrize(
    "title",
    [
        "PC Gaming i7-14700kf 32GB RAM RTX 4070 Super",
        "Ordenador gaming Ryzen 5 + RTX 3070",
        "Torre gaming completa con RTX 4060 Ti",
        "PC completo RTX 3080",
    ],
)
def test_whole_pc_bundles_are_excluded(title):
    """A tower priced at 850 EUR is not a comp for a loose GPU."""
    verdict = junk.check(title)
    assert verdict.excluded and verdict.category == "BUNDLE"


@pytest.mark.parametrize(
    "title",
    [
        "Tarjeta gráfica RTX 4070 para PC gaming",
        "Gráfica RTX 3070 ideal para pc gaming",
        "GPU RTX 4060 Ti sacada de un pc gaming",
    ],
)
def test_cards_that_merely_mention_a_pc_survive(title):
    assert not junk.check(title).excluded


@pytest.mark.parametrize(
    "title",
    [
        "Lenovo Legion Pro 5 rtx 4070",
        "Portátil Asus TUF F15 RTX 4070",
        "AORUS 17 RTX4070 - 2K 240Hz 16gb DDR5",
        "MSI Sword 17 HX RTX 4070 i7 14650HX",
        "ASUS ProArt P16 – RTX 4070 / 64GB RAM / 2TB SSD",
        "Asus TUF A15 Ryzen 9 RTX 4070 32GB RAM",
        "Lenovo Legion 7i Gen 9 (RTX 4070)",
        "ALIENWARE M16 R2 RTX 4070",
        "HP Victus 16 RTX 4060",
    ],
)
def test_laptops_are_excluded(title):
    """Real titles from the live API — a 2600 EUR laptop is not a 4070 comp."""
    verdict = junk.check(title)
    assert verdict.excluded and verdict.category == "LAPTOP"


@pytest.mark.parametrize(
    "title",
    [
        # The AIB brand words deliberately kept out of the laptop token list.
        "RX 7900 XTX Sapphire Nitro+ 16GB",
        "RTX 4070 ti asus tuf gaming oc 12gb",
        "ASUS ROG Strix RTX 3080 OC",
        "Gigabyte RTX 4070 AORUS Elite 12GB",
        "Sapphire RX 7800 XT Pulse",
        "MSI RTX 4070 Ventus 3X",
        "Powercolor RX 9060 XT Hellhound 16GB",
        "Tarjeta gráfica RTX 3070 de un portátil",
    ],
)
def test_desktop_cards_are_not_mistaken_for_laptops(title):
    verdict = junk.check(title)
    assert not verdict.excluded, f"wrongly excluded on {verdict.phrase!r} ({verdict.category})"


@pytest.mark.parametrize(
    "title",
    [
        "ASUS ProArt P16 – RTX 4070 / 64GB RAM / 2TB SSD",
        "Asus ProArt PX13 Ryzen 9 RTX 4060",
        "Portátil Asus ProArt Studiobook 16",
    ],
)
def test_proart_laptops_are_excluded(title):
    """ProArt P16/PX13 are laptop model numbers, unlike bare 'ProArt'."""
    verdict = junk.check(title)
    assert verdict.excluded and verdict.category == "LAPTOP"


def test_proart_graphics_card_is_not_mistaken_for_a_laptop():
    """ProArt is also a real ASUS desktop-GPU sub-brand — must survive."""
    verdict = junk.check("ASUS ProArt GeForce RTX 4080 OC 16GB")
    assert not verdict.excluded, f"wrongly excluded on {verdict.phrase!r}"


def test_exclusion_reports_phrase_and_category():
    verdict = junk.check("RTX 3070 para piezas")
    assert verdict.phrase == "para piezas"
    assert verdict.category == "DEFECT"


@pytest.mark.parametrize(
    "title",
    [
        "Procesador AMD Ryzen 5 7600 6 nucleos 12 hilos",
        "Microprocesador Ryzen 5 5600X",
    ],
)
def test_cpu_listings_are_excluded(title):
    """Regression: Ryzen 5 7600 (CPU) numerically collides with Radeon RX 7600
    (GPU) with no differentiating suffix on either side — a real production
    listing was misclassified as a confident 'DEAL' on RX 7600 before this fix.
    The filter is deliberately narrow ("procesador"/"microprocesador" only, not
    the bare "ryzen"/"amd" brand names) so it doesn't cost real GPU listings
    that merely mention CPU compatibility.
    """
    verdict = junk.check(title)
    assert verdict.excluded and verdict.category == "CPU"


def test_desktop_cpu_model_numbers_are_excluded():
    """Ryzen and Radeon model numbers collide exactly ("Ryzen 5 7600" vs
    "RX 7600"), and "amd" is a valid AMD brand token, so an unfiltered
    "AMD Ryzen 5 7600X" classified as an RX 7600 at *high* confidence and got
    priced against a ~200 EUR reference. Verified live 2026-08-26."""
    for title in (
        "AMD Ryzen 7 7700X sin usar",
        "AMD Ryzen 5 7600X",
        "Ryzen 5 7600",
        "Intel i5 12400F",
        "Intel Core i9 13900K",
    ):
        assert junk.check(title).excluded, title


def test_a_gpu_title_naming_a_cpu_still_survives():
    """The rule is skipped when the title names a GPU vendor, so a card sold
    as compatible with a processor is not mistaken for one."""
    for title in (
        "RX 6700 XT compatible con Ryzen 7 5800X",
        "RTX 4070 ideal para Ryzen 5 7600",
        "Tarjeta grafica RTX 4070 ideal para Ryzen 5000",
    ):
        assert not junk.check(title).excluded, title


def test_card_mentioning_a_compatible_cpu_survives():
    verdict = junk.check("Tarjeta grafica RTX 4070 ideal para Ryzen 5000")
    assert not verdict.excluded, f"wrongly excluded on {verdict.phrase!r}"


# ------------------------------------------------ seed-confidence penalty
def _row(ref, is_seed):
    return {
        "ref_price": ref,
        "buy_ceiling": config.SHIPPED.buy_ceiling(ref),
        "buy_ceiling_in_person": config.IN_PERSON.buy_ceiling(ref),
        "is_seed": is_seed,
        "n_comps": 0 if is_seed else 12,
    }


def test_seeded_models_demand_more_margin_than_learned_ones():
    """A seed price is a guess, and the whole feed is only as good as it: a seed
    25% too high makes every ordinary listing look like a bargain. So confidence
    buys leniency — and this relaxes by itself once real comps arrive."""
    ref = 560.0
    # 500 sits between the two ceilings: an offer of 400 clears the learned
    # ceiling (~422) but not the seeded one (~366).
    learned = pricing.evaluate(500.0, _row(ref, is_seed=False), None)
    seeded = pricing.evaluate(500.0, _row(ref, is_seed=True), None)
    assert learned.qualifies
    assert not seeded.qualifies
    assert seeded.ceiling_shipped < learned.ceiling_shipped


def test_a_genuine_bargain_still_gets_through_a_seed_price():
    """The penalty raises the bar, it does not close the door — otherwise a
    brand-new model could never produce its first alert.

    Note how narrow the door has become. On a seeded 560 EUR reference the
    plausibility floor puts the bottom at 364 EUR and the seed-penalised ceiling
    puts the top at ~457 EUR, so a first alert has to land in a ~93 EUR window.
    """
    seeded = pricing.evaluate(400.0, _row(560.0, is_seed=True), None)
    assert seeded.qualifies


def test_seed_penalty_can_be_disabled(monkeypatch):
    monkeypatch.setattr(config, "SEED_MARGIN_MULTIPLIER", 1.0)
    assert pricing.evaluate(500.0, _row(560.0, is_seed=True), None).qualifies


def test_an_extremely_cheap_listing_is_now_rejected():
    """MIN_PLAUSIBLE_RATIO is 0.65 since 2026-08-26, reversing the earlier
    decision to disable it. A 150 EUR listing against a 700 EUR reference is
    exactly the shape this guard exists for — and exactly the shape of a genuine
    drawer-clearing steal, which is the price of having it on."""
    deal = pricing.evaluate(150.0, _row(700.0, is_seed=False), None)
    assert not deal.qualifies
    assert "implausibly cheap" in deal.reason
    assert deal.ref_price == 700.0


def test_the_plausibility_floor_is_the_binding_lower_bound():
    """MIN_SANE_PRICE (50) is no longer what stops a cheap listing — the ratio
    is, and it bites far higher. On a 700 EUR reference the floor is 455 EUR, so
    everything from 50 to 454 is now rejected without being evaluated."""
    assert not pricing.evaluate(454.0, _row(700.0, is_seed=False), None).qualifies
    assert pricing.evaluate(455.0, _row(700.0, is_seed=False), None).qualifies


def test_the_ratio_is_a_setting_not_a_hardcoded_rule(monkeypatch):
    """Guards the two tests above from silently becoming vacuous: the gate reads
    the config value, so setting it back to 0 restores the old behaviour."""
    monkeypatch.setattr(pricing.config, "MIN_PLAUSIBLE_RATIO", 0.0)
    assert pricing.evaluate(150.0, _row(700.0, is_seed=False), None).qualifies
