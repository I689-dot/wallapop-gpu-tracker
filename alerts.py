"""Telegram delivery (raw Bot API over httpx — no extra dependency needed)."""

from __future__ import annotations

import html
import logging
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

import config
from pricing import Deal
from wallapop_client import Item

log = logging.getLogger("alerts")

API = "https://api.telegram.org/bot{token}/{method}"
MADRID = ZoneInfo("Europe/Madrid")

# Telegram truncates photo captions at 1024 characters.
CAPTION_LIMIT = 1024
DESC_CHARS = 200

# How much later than `posted_at` a `modified_at` has to be before it counts as
# a real edit. Wallapop stamps both on creation, and they are not always
# byte-identical, so a bare inequality would print "edited" on every untouched
# listing in the feed and the signal would mean nothing.
MODIFIED_EPSILON_SECONDS = 120

# The API's condition enum, in the words Wallapop itself shows the seller.
# Printing the raw value ("as_good_as_new") in an alert is noise; an unknown
# key falls through unchanged so a new tier never blanks the line.
CONDITION_ES = {
    "un_opened": "Sin abrir",
    "in_box": "En caja",
    "new": "Nuevo",
    "as_good_as_new": "Como nuevo",
    "good": "Buen estado",
    "fair": "Aceptable",
    "has_given_it_all": "Ha dado lo mejor de si",
}


def _eur(value: float | None) -> str:
    if value is None:
        return "?"
    return f"{value:,.0f}€".replace(",", ".")


def _signed(value: float | None) -> str:
    if value is None:
        return "?"
    return f"{value:+,.0f}€".replace(",", ".")


def _age_str(seconds: float | None) -> str | None:
    """Human phrasing of listing age. The owner cares a lot about recency,
    so this gets its own line whenever posted_at/age_seconds is available.
    """
    if seconds is None or seconds < 0:
        return None
    minutes = seconds / 60
    if minutes < 1:
        return "hace <1 min"
    if minutes < 60:
        return f"hace {round(minutes)} min"
    hours = minutes / 60
    if hours < 24:
        return f"hace {round(hours)} h"
    return f"hace {round(hours / 24)} d"


def _edited_str(item: Item) -> str | None:
    """"Seller touched this recently", when the API says they did.

    `modified_at` was parsed off every listing and then read by nothing at all.
    It is worth a few characters of caption for two reasons the rest of the
    alert cannot supply:

      * a price cut on a listing that never alerted is otherwise invisible.
        `_decide_kind` can only compare against our own alert history, so a
        seller who lists at 400, gets no interest and drops to 260 shows up as a
        plain "new" match with nothing to say that the price just moved;
      * a recent edit means a live seller, which is most of whether an offer
        gets answered at all — the same reason "Publicado hace 4 min" earns its
        place on the line above.

    Returns None unless the edit is meaningfully later than the listing date, so
    an untouched listing prints nothing. Read through getattr and guarded
    against a naive datetime, in the same defensive register as the rest of this
    file.
    """
    modified_at = getattr(item, "modified_at", None)
    if modified_at is None:
        return None
    posted_at = getattr(item, "posted_at", None)
    try:
        if (
            posted_at is not None
            and (modified_at - posted_at).total_seconds() < MODIFIED_EPSILON_SECONDS
        ):
            return None
        age = (datetime.now(timezone.utc) - modified_at).total_seconds()
    except TypeError:  # naive/aware mismatch — no usable signal
        return None
    phrase = _age_str(age)
    return f"Editado {phrase}" if phrase else None


def build_caption(
    item: Item,
    deal: Deal,
    kind: str,
    previous_price: float | None,
    model_display: str | None,
) -> str:
    """The §9 alert body, as Telegram HTML.

    No emoji anywhere — plain-text labels instead, so it reads as a normal
    message rather than a wall of icons. Bold is reserved for the numbers
    that matter (prices, margins) so the caption stays scannable.
    """
    if kind == "price_drop":
        head = "<b>BAJADA DE PRECIO</b>"
    elif deal.priced:
        head = "<b>CHOLLO</b>"
    else:
        head = "<b>COINCIDENCIA</b>"
    if model_display:
        head += f" · {html.escape(model_display)}"

    lines = [head]

    price_line = f"Precio: <b>{_eur(item.price)}</b>"
    if previous_price and item.price is not None and previous_price > item.price:
        price_line += f"  (antes <s>{_eur(previous_price)}</s>)"
    lines.append(price_line)

    if deal.priced:
        lines.append(
            f"Oferta sugerida: <b>{_eur(deal.offer_price)}</b> · "
            f"neto envío {_signed(deal.net_shipped)} / mano {_signed(deal.net_in_person)}"
        )
        margin = f"Ref: <b>{_eur(deal.ref_price)}</b> · Techo: <b>{_eur(deal.ceiling_shipped)}</b>"
        if deal.net_shipped_at_asking is not None and deal.net_shipped_at_asking >= 0:
            margin += f" · a precio pedido: <b>{_signed(deal.net_shipped_at_asking)}</b>"
        if deal.is_seed:
            margin += "  <i>(seed)</i>"
        elif deal.n_comps:
            # n_comps is printed as provenance — "this reference rests on 8
            # transactions" — and it was overstating what is actually known
            # about *this* model. A generic key short of MIN_COMPS borrows comps
            # from its split-VRAM siblings, so a model owning zero comps of its
            # own could report n=8. That is still the honest best estimate, but
            # it is a different claim, and the caption is where the owner
            # decides whether to trust the number. Shown only when the two
            # differ, so the common case stays as short as it was.
            #
            # getattr because n_own is a recent addition to the pricing path; a
            # Deal without it falls back to exactly the old line. A genuine 0 is
            # meaningful here (it is the whole point), so only None means
            # "unknown".
            n_own = getattr(deal, "n_own", None)
            if n_own is not None and int(n_own) != int(deal.n_comps):
                margin += f"  <i>(n={deal.n_comps}, {int(n_own)} propios)</i>"
            else:
                margin += f"  <i>(n={deal.n_comps})</i>"
        lines.append(margin)

    # Comp-pool provenance (median_days_to_sale / n_sold / n_reserved) is
    # landing on Deal from a concurrent agent's pricing.py work — read
    # defensively so this file doesn't break if the shape differs. n_sold and
    # n_reserved default to 0 rather than None on that dataclass, and 0 comps
    # means "no signal" here, so treat them (and median_days_to_sale) as
    # absent whenever they're falsy and omit the whole line rather than
    # print a placeholder.
    median_days = getattr(deal, "median_days_to_sale", None)
    n_sold = getattr(deal, "n_sold", None)
    n_reserved = getattr(deal, "n_reserved", None)
    comp_bits = []
    if median_days is not None:
        comp_bits.append(f"venta media {median_days:.0f} días")
    if n_sold:
        comp_bits.append(f"{n_sold} vendidos")
    if n_reserved:
        comp_bits.append(f"{n_reserved} reservados")
    if comp_bits:
        lines.append("Histórico: " + " · ".join(comp_bits))

    # Same story for Item: condition/brand/posted_at are a concurrent
    # agent's addition, so getattr defends against the field being missing.
    detail_bits = []
    condition = getattr(item, "condition", None)
    if condition:
        detail_bits.append(f"Estado: {html.escape(CONDITION_ES.get(condition, condition))}")
    brand = getattr(item, "brand", None)
    if brand:
        detail_bits.append(f"Marca: {html.escape(str(brand))}")
    age = _age_str(getattr(item, "age_seconds", None))
    if age:
        detail_bits.append(f"Publicado {age}")
    edited = _edited_str(item)
    if edited:
        detail_bits.append(edited)
    if detail_bits:
        lines.append(" · ".join(detail_bits))

    where = []
    if item.location:
        where.append(html.escape(str(item.location)))
    if item.country and item.country.upper() != "ES":
        where.append(html.escape(item.country.upper()))
    if item.distance_km is not None:
        try:
            where.append(f"{float(item.distance_km):.0f} km")
        except (TypeError, ValueError):
            pass
    # can_ship reflects the seller's actual choice on this listing; `shipping`
    # is only the category's general capability, so prefer can_ship when the
    # (concurrent-agent-added) field is present.
    can_ship = getattr(item, "can_ship", None)
    if can_ship is None:
        can_ship = item.shipping
    where.append("envío disponible" if can_ship else "solo en mano")
    lines.append("Ubicación: " + " · ".join(where))

    lines.append("Hora: " + datetime.now(MADRID).strftime("%Y-%m-%d %H:%M"))
    lines.append("")
    lines.append(f"<b>{html.escape(item.title[:150])}</b>")

    if item.description:
        desc = " ".join(item.description.split())[:DESC_CHARS]
        lines.append(html.escape(desc))

    lines.append("")
    lines.append(html.escape(item.web_url))

    caption = "\n".join(lines)
    if len(caption) > CAPTION_LIMIT:
        overflow = len(caption) - CAPTION_LIMIT + 3
        # Trim the description, never the link or the numbers.
        if item.description and overflow < DESC_CHARS:
            # lines[-3] is the description: [..., desc, "", link]
            lines[-3] = lines[-3][: max(0, len(lines[-3]) - overflow)] + "…"
            caption = "\n".join(lines)
        else:
            # Every line's HTML tags open and close within that same line, so
            # cutting at the last newline can never sever one — a mid-tag cut
            # would make Telegram reject the whole message as unparsable HTML.
            truncated = caption[: CAPTION_LIMIT - 1]
            safe_cut = truncated.rfind("\n")
            caption = (truncated[:safe_cut] if safe_cut > 0 else truncated) + "…"
    return caption


def build_error_text(text: str) -> str:
    """Error-ping body. No emoji, same as the deal caption — a bare text
    label reads just as clearly as a warning icon and stays consistent.
    """
    return f"<b>wallapop-bot</b>\n<pre>{html.escape(text[:1500])}</pre>"


# --------------------------------------------------- repeat-ping suppression
# A fault that persists reports once per cooldown, not once per run. The
# dead-man switch learned this the hard way — see alert_loop's note about a
# weekend outage being ~1000 identical messages — and a crashing loop has
# exactly the same shape: the comps loop timed out on roughly every other run
# through early October 2026 and sent an identical traceback each time.
#
# State lives in `run_log.notes` rather than in memory because every run is a
# fresh process under GitHub Actions, so there is nothing else that survives
# between them.
def parse_run_time(value: object) -> datetime | None:
    """Best-effort parse of a run_log timestamp; Supabase returns ISO strings."""
    if value is None:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def marked_within(runs: list[dict], marker: str, cutoff: datetime) -> bool:
    """Whether any run at or after `cutoff` already carried `marker` in notes."""
    for run in runs:
        if marker not in (run.get("notes") or ""):
            continue
        stamp = parse_run_time(run.get("started_at") or run.get("finished_at"))
        # An unparsable timestamp on a marked row counts as recent. Every row
        # here came out of the newest-first history window, so it is recent by
        # construction; the only question was whether it is inside the cooldown,
        # and for a warning that otherwise repeats forever, staying quiet is the
        # safer answer to "we can't tell".
        if stamp is None or stamp >= cutoff:
            return True
    return False


class Telegram:
    def __init__(self, token: str | None = None, chat_id: str | None = None) -> None:
        self.token = token or config.TELEGRAM_TOKEN
        self.chat_id = chat_id or config.TELEGRAM_CHAT_ID
        self._client = httpx.Client(timeout=config.HTTP_TIMEOUT)
        self._last_send = 0.0

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Telegram":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_send
        if elapsed < config.TELEGRAM_RATE_DELAY:
            time.sleep(config.TELEGRAM_RATE_DELAY - elapsed)
        self._last_send = time.monotonic()

    def _post(self, method: str, payload: dict) -> bool:
        url = API.format(token=self.token, method=method)
        try:
            resp = self._client.post(url, json=payload)
        except httpx.HTTPError as exc:
            log.error("Telegram %s failed: %s", method, exc)
            return False
        if resp.status_code == 200:
            return True
        log.error("Telegram %s -> %s: %s", method, resp.status_code, resp.text[:300])
        return False

    def send_alert(
        self,
        item: Item,
        deal: Deal,
        kind: str,
        previous_price: float | None = None,
        model_display: str | None = None,
    ) -> bool:
        caption = build_caption(item, deal, kind, previous_price, model_display)

        if config.DRY_RUN:
            log.info("[DRY RUN] would send:\n%s", caption)
            return True

        self._throttle()
        if item.image_url:
            sent = self._post(
                "sendPhoto",
                {
                    "chat_id": self.chat_id,
                    "photo": item.image_url,
                    "caption": caption,
                    "parse_mode": "HTML",
                },
            )
            if sent:
                return True
            # A rejected image URL must not cost us the alert.
            log.warning("sendPhoto failed for %s — falling back to text", item.item_id)

        return self._post(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": caption,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
        )

    def send_error(self, text: str) -> None:
        """Surface failures instead of dying silently in a cron runner."""
        if config.DRY_RUN:
            log.info("[DRY RUN] error ping: %s", text)
            return
        self._throttle()
        self._post(
            "sendMessage",
            {
                "chat_id": self.chat_id,
                "text": build_error_text(text),
                "parse_mode": "HTML",
            },
        )
