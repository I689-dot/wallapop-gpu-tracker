"""Alert caption formatting, especially the 1024-char truncation edge case.

These pin `build_caption`/`build_error_text`, which still emit a small balanced
subset of HTML; `Discord` converts that to Markdown at send time (see
tests/test_discord.py). Balanced tags matter more now, not less: the converter
trusts them.
"""

import re
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from alerts import CAPTION_LIMIT, build_caption, build_error_text  # noqa: E402
from pricing import Deal  # noqa: E402
from wallapop_client import Item  # noqa: E402

# Unicode blocks that cover essentially every emoji in normal use, plus the
# variation selector (U+FE0F) that turns some base characters into emoji
# presentation. A regression test walks every codepoint in the caption
# against these ranges rather than checking for a handful of literal glyphs,
# so it catches *any* emoji the owner didn't ask for, not just the old ones.
EMOJI_RANGES = (
    (0x1F300, 0x1F5FF),  # Miscellaneous Symbols and Pictographs
    (0x1F600, 0x1F64F),  # Emoticons
    (0x1F680, 0x1F6FF),  # Transport and Map Symbols
    (0x1F900, 0x1F9FF),  # Supplemental Symbols and Pictographs
    (0x2600, 0x26FF),    # Miscellaneous Symbols
    (0x2700, 0x27BF),    # Dingbats
)
VARIATION_SELECTOR_16 = 0xFE0F


def find_emoji(text: str) -> list[str]:
    """Return every character in `text` that falls in an emoji block."""
    hits = []
    for ch in text:
        cp = ord(ch)
        if cp == VARIATION_SELECTOR_16 or any(lo <= cp <= hi for lo, hi in EMOJI_RANGES):
            hits.append(ch)
    return hits

DEAL = Deal(
    qualifies=True,
    reason="offer clears buy ceiling",
    ref_price=330.0,
    ceiling_shipped=224.0,
    ceiling_in_person=280.0,
    offer_price=168.0,
    net_shipped=55.0,
    net_in_person=120.0,
    net_shipped_at_asking=19.0,
    is_seed=False,
    n_comps=12,
)


def make_item(**overrides):
    base = dict(
        item_id="x1",
        title="RTX 4070 Gigabyte Gaming OC",
        description="Estado impecable, poco uso, con caja y factura original.",
        price=210.0,
        currency="EUR",
        web_url="https://es.wallapop.com/item/rtx-4070-gigabyte-123",
        image_url="https://cdn/x.jpg",
        reserved=False,
        shipping=True,
        location="Madrid",
        distance_km=4.0,
        country="ES",
    )
    base.update(overrides)
    return Item(**base)


def has_unbalanced_tags(text: str) -> bool:
    """True if any <b>/<s>/<i> tag has no matching close in the same text."""
    for tag in ("b", "s", "i"):
        opens = len(re.findall(rf"<{tag}>", text))
        closes = len(re.findall(rf"</{tag}>", text))
        if opens != closes:
            return True
    return False


def test_normal_caption_is_well_formed():
    caption = build_caption(make_item(), DEAL, "new", None, "RTX 4070")
    assert len(caption) <= CAPTION_LIMIT
    assert not has_unbalanced_tags(caption)
    assert "RTX 4070 Gigabyte Gaming OC" in caption


def test_long_description_is_trimmed_not_the_link():
    item = make_item(description="x" * 5000)
    caption = build_caption(item, DEAL, "new", None, "RTX 4070")
    assert len(caption) <= CAPTION_LIMIT
    assert not has_unbalanced_tags(caption)
    assert item.web_url in caption  # link survives even when description is huge


def test_extreme_overflow_falls_back_to_safe_line_truncation():
    """A location/title long enough to overflow even with an empty description
    must still produce a caption whose tags balance — no tag split mid-cut.
    """
    item = make_item(
        title="RTX 4070 " + "Z" * 400,
        location="M" * 400,
        description="",
    )
    caption = build_caption(item, DEAL, "new", None, "RTX 4070")
    assert len(caption) <= CAPTION_LIMIT + 1  # +1 for the trailing ellipsis char
    assert not has_unbalanced_tags(caption)


def test_price_drop_shows_strikethrough_previous():
    caption = build_caption(make_item(price=180.0), DEAL, "price_drop", 210.0, "RTX 4070")
    assert "<s>210€</s>" in caption
    assert not has_unbalanced_tags(caption)


def test_new_listing_never_shows_a_previous_price():
    caption = build_caption(make_item(), DEAL, "new", None, "RTX 4070")
    assert "<s>" not in caption


def test_html_special_characters_in_title_are_escaped():
    item = make_item(title="RTX 4070 <script>alert(1)</script> & Co")
    caption = build_caption(item, DEAL, "new", None, "RTX 4070")
    assert "<script>" not in caption
    assert "&lt;script&gt;" in caption


def test_offer_price_is_shown_for_a_priced_deal():
    caption = build_caption(make_item(), DEAL, "new", None, "RTX 4070")
    assert "168€" in caption  # the suggested offer, not just the asking price


def test_foreign_country_is_shown_in_location_line():
    caption = build_caption(make_item(country="IT", location="Milano"), DEAL, "new", None, "RTX 4070")
    assert "IT" in caption


def test_spain_is_not_called_out_as_foreign():
    caption = build_caption(make_item(country="ES"), DEAL, "new", None, "RTX 4070")
    location_line = next(line for line in caption.splitlines() if line.startswith("Ubicación:"))
    segments = [s.strip() for s in location_line.split("·")]
    assert "ES" not in segments


# --------------------------------------------------------------- no emoji


def test_caption_contains_no_emoji_anywhere():
    """Hard regression: the owner explicitly asked for a zero-emoji alert."""
    caption = build_caption(make_item(), DEAL, "new", None, "RTX 4070")
    assert find_emoji(caption) == []


def test_price_drop_caption_contains_no_emoji():
    caption = build_caption(make_item(price=180.0), DEAL, "price_drop", 210.0, "RTX 4070")
    assert find_emoji(caption) == []


def test_plain_match_caption_contains_no_emoji():
    unpriced = Deal(qualifies=True, reason="under bootstrap cap")
    caption = build_caption(make_item(), unpriced, "new", None, None)
    assert find_emoji(caption) == []


def test_error_text_contains_no_emoji():
    text = build_error_text("boom: something failed")
    assert find_emoji(text) == []
    assert "wallapop-bot" in text
    assert "boom: something failed" in text


# --------------------------------------------------------- optional fields


def test_condition_and_brand_shown_when_present():
    item = make_item(condition="Como nuevo", brand="Gigabyte")
    caption = build_caption(item, DEAL, "new", None, "RTX 4070")
    assert "Estado: Como nuevo" in caption
    assert "Marca: Gigabyte" in caption


def test_condition_and_brand_omitted_when_absent():
    caption = build_caption(make_item(), DEAL, "new", None, "RTX 4070")
    assert "Estado:" not in caption
    assert "Marca:" not in caption


def test_posted_recency_shown_when_posted_at_present():
    posted = datetime.now(timezone.utc) - timedelta(minutes=4)
    item = make_item(posted_at=posted)
    caption = build_caption(item, DEAL, "new", None, "RTX 4070")
    assert "Publicado hace 4 min" in caption


def test_posted_recency_omitted_when_posted_at_absent():
    caption = build_caption(make_item(), DEAL, "new", None, "RTX 4070")
    assert "Publicado" not in caption


def test_comp_provenance_shown_when_fields_present():
    """median_days_to_sale / n_sold / n_reserved land on Deal from a
    concurrent agent's work; read them defensively and show them when set.
    """
    priced_deal = replace(DEAL)
    priced_deal.median_days_to_sale = 6
    priced_deal.n_sold = 8
    priced_deal.n_reserved = 3
    caption = build_caption(make_item(), priced_deal, "new", None, "RTX 4070")
    assert "Histórico:" in caption
    assert "venta media 6 días" in caption
    assert "8 vendidos" in caption
    assert "3 reservados" in caption


def test_comp_provenance_omitted_when_fields_absent():
    caption = build_caption(make_item(), DEAL, "new", None, "RTX 4070")
    assert "Histórico:" not in caption


def test_can_ship_preferred_over_category_shipping_flag():
    """A seller who disabled shipping (`user_allows_shipping=False`) must show
    as hand-only, even though the category itself is shippable (`shipping=True`).
    """
    item = make_item(shipping=True, user_allows_shipping=False)
    caption = build_caption(item, DEAL, "new", None, "RTX 4070")
    assert "solo en mano" in caption
    assert "envío disponible" not in caption


def test_can_ship_falls_back_to_shipping_flag_when_unset():
    item = make_item(shipping=True)  # user_allows_shipping left at its default (None)
    caption = build_caption(item, DEAL, "new", None, "RTX 4070")
    assert "envío disponible" in caption


# ------------------------------------------------------- comp-pool provenance


def test_own_and_total_comp_counts_are_both_shown_when_they_differ():
    """n_comps was overstating what is known about *this* model.

    A generic key short of MIN_COMPS borrows comps from its split-VRAM
    siblings, so a model owning zero comps of its own could print n=12 as
    provenance. That is still the honest best estimate, but it is a different
    claim, and the caption is where the owner decides how much to trust it.
    """
    deal = replace(DEAL)
    deal.n_own = 0
    caption = build_caption(make_item(), deal, "new", None, "RTX 4070")
    assert "n=12, 0 propios" in caption


def test_only_the_total_is_shown_when_all_comps_are_the_model_s_own():
    """The common case has to stay as short as it was."""
    deal = replace(DEAL)
    deal.n_own = 12
    caption = build_caption(make_item(), deal, "new", None, "RTX 4070")
    assert "n=12)" in caption
    assert "propios" not in caption


def test_unknown_n_own_falls_back_to_the_plain_count():
    """n_own arrives on Deal from a concurrent change to the pricing path, so it
    is read through getattr and None means "we don't know", not "zero" — a
    genuine 0 is the interesting case and must stay distinguishable from an
    absent field."""
    deal = replace(DEAL)
    deal.n_own = None
    caption = build_caption(make_item(), deal, "new", None, "RTX 4070")
    assert "n=12)" in caption
    assert "propios" not in caption


# ---------------------------------------------------------- modification recency


def test_recent_edit_is_surfaced():
    """`modified_at` was parsed off every listing and read by nothing.

    It is the only signal for "the seller just cut the price" on a listing that
    never alerted — `_decide_kind` can only compare against our own alert
    history — and a recent edit also means a live seller, which is most of
    whether an offer gets answered.
    """
    now = datetime.now(timezone.utc)
    item = make_item(posted_at=now - timedelta(days=6), modified_at=now - timedelta(hours=3))
    caption = build_caption(item, DEAL, "new", None, "RTX 4070")
    assert "Editado hace 3 h" in caption


def test_untouched_listing_shows_no_edit():
    """Wallapop stamps modified_at on creation too, so a bare inequality would
    print 'edited' on every listing in the feed and the signal would mean
    nothing."""
    now = datetime.now(timezone.utc)
    item = make_item(posted_at=now - timedelta(hours=2), modified_at=now - timedelta(hours=2))
    caption = build_caption(item, DEAL, "new", None, "RTX 4070")
    assert "Editado" not in caption


def test_missing_modified_at_shows_no_edit():
    caption = build_caption(make_item(), DEAL, "new", None, "RTX 4070")
    assert "Editado" not in caption


def test_naive_modified_at_is_ignored_rather_than_raising():
    item = make_item(modified_at=datetime(2026, 8, 1, 12, 0))  # no tzinfo
    caption = build_caption(item, DEAL, "new", None, "RTX 4070")
    assert "Editado" not in caption
    assert len(caption) <= CAPTION_LIMIT


def test_edit_line_does_not_break_the_truncation_order():
    """The truncation logic assumes lines[-3] is the description, so anything
    added to the detail line has to leave that shape intact."""
    now = datetime.now(timezone.utc)
    item = make_item(
        description="x" * 5000,
        posted_at=now - timedelta(days=6),
        modified_at=now - timedelta(minutes=30),
        condition="good",
        brand="Gigabyte",
    )
    caption = build_caption(item, DEAL, "new", None, "RTX 4070")
    assert len(caption) <= CAPTION_LIMIT
    assert not has_unbalanced_tags(caption)
    assert item.web_url in caption
    assert "Editado hace 30 min" in caption
