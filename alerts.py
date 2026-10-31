"""Discord delivery (bot token over the plain REST API — no extra dependency).

The alert text is still *built* as a small, balanced subset of HTML
(`build_caption`, `build_error_text`) and converted to Discord Markdown only at
the moment of sending (`html_to_markdown`). That split is deliberate: the
builders were written for Telegram and have ~40 tests pinning their output, and
Discord renders its own Markdown rather than HTML, so the translation lives in
one place at the edge instead of the text being rewritten twice.

A bot posts with `Authorization: Bot <token>` to
`/channels/{channel_id}/messages`. No gateway connection is needed to *send*, so
there is no long-lived process and nothing to keep alive between loop runs.
"""

from __future__ import annotations

import html
import logging
import re
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import httpx

import config
from pricing import Deal
from wallapop_client import Item

log = logging.getLogger("alerts")

MADRID = ZoneInfo("Europe/Madrid")

# Inherited from Telegram, whose photo captions truncate at 1024 characters.
# Discord's embed description allows 4096, but the cap is kept as a conservative
# bound on how long one alert gets: a phone's notification preview is not a place
# for a full listing description, and the tests pin the behaviour.
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
    """The §9 alert body, as a small balanced subset of HTML.

    Converted to Discord Markdown by `html_to_markdown` on the way out; every tag opens
    and closes within the line that carries it, which is what makes that
    conversion (and the truncation below) safe.

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
            # would leave an unbalanced tag for html_to_markdown to mis-render.
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


# ------------------------------------------------- HTML -> Markdown (the edge)
# Only the tags build_caption / build_error_text emit are handled. Everything
# else in a message is seller-controlled free text that html.escape has already
# neutralised, so a real tag can only be one we wrote.
_TAG_RX = re.compile(r"(</?(?:b|i|s|pre)>)")
_URL_LINE_RX = re.compile(r"^https?://\S+$")
# What would turn seller text into formatting. `_` and `*` are the real risk
# (a listing titled "RTX_3070 *URGE*"); the rest keep headings, quotes, spoilers
# (`||`), links and `<@id>` mention syntax from appearing out of nowhere.
_MD_SPECIALS_RX = re.compile(r"([\\`*_~|>#<\[\]])")
_TAG_STRIP_RX = re.compile(r"</?(?:b|i|s|pre)>")


def _md_escape(text: str) -> str:
    return _MD_SPECIALS_RX.sub(r"\\\1", text)


def _fence_for(code: str) -> str:
    """A code fence longer than any backtick run inside the code."""
    longest = max((len(m) for m in re.findall(r"`+", code)), default=0)
    return "`" * max(3, longest + 1)


def html_to_markdown(text: str) -> str:
    """Convert the caption/error HTML subset into Discord Markdown.

    * `<b>` -> bold, `<i>` -> italic, `<s>` -> strikethrough, so the caption's
      "(antes ~~260€~~)" keeps its meaning.
    * `<pre>` -> a fenced code block, contents untouched (tracebacks are full of
      characters Markdown would otherwise eat).
    * Outside `<pre>`, text is entity-unescaped and then Markdown-escaped.
      Newlines stay plain: Discord, unlike CommonMark, treats a single newline
      as a line break.
    * A line that is only a URL is left unescaped: backslash escapes would
      corrupt the link, and Discord autolinks a bare URL.
    """
    out: list[str] = []
    in_pre = False
    pre_buf: list[str] = []
    for part in _TAG_RX.split(text):
        if part == "<pre>":
            in_pre, pre_buf = True, []
        elif part == "</pre>":
            in_pre = False
            code = html.unescape("".join(pre_buf))
            fence = _fence_for(code)
            out.append(f"{fence}\n{code}\n{fence}")
        elif in_pre:
            pre_buf.append(part)
        elif part in ("<b>", "</b>"):
            out.append("**")
        elif part in ("<i>", "</i>"):
            out.append("*")
        elif part in ("<s>", "</s>"):
            out.append("~~")
        else:
            lines = []
            for line in html.unescape(part).split("\n"):
                lines.append(line if _URL_LINE_RX.match(line) else _md_escape(line))
            out.append("\n".join(lines))
    return "".join(out)


def split_title(text: str) -> tuple[str, str]:
    """Split the first line off as the embed title; the rest is the description.

    "CHOLLO · RTX 3070" belongs in the title, which is what a notification
    preview shows. The title is plain text: tags stripped, entities decoded.
    """
    head, _, rest = text.partition("\n")
    title = html.unescape(_TAG_STRIP_RX.sub("", head)).strip()
    return title, rest.lstrip("\n")


API = "https://discord.com/api/v10"

# Discord's hard limits on an embed. Exceeding either is a 400 that loses the
# whole alert, so both are enforced rather than trusted to the caption cap.
EMBED_TITLE_LIMIT = 256
EMBED_DESCRIPTION_LIMIT = 4096

# Embed accent colours: green for a deal, amber for a price drop, grey for a bare
# keyword match, red for an error ping. Colour is the one thing visible from the
# channel list at a glance, which is what the three situations differ by.
COLOR_DEAL = 0x2ECC71
COLOR_DROP = 0xF1C40F
COLOR_MATCH = 0x95A5A6
COLOR_ERROR = 0xE74C3C

# On a 429, wait out Discord's `retry_after` if it is short enough to be worth
# blocking a loop run for, and give up beyond that: a failed alert is not
# recorded in sent_alerts, so it is retried on the next cycle anyway.
MAX_RETRY_WAIT_SECONDS = 10.0
SEND_ATTEMPTS = 3


class Discord:
    """Sends alerts and error pings to one Discord channel as a bot.

    Setup is the usual bot dance — create an application, add a bot, invite it to
    the server with *Send Messages* and *Embed Links* in the target channel — and
    the failure for getting it wrong is a 403 with a Discord error code, which is
    logged verbatim because it names the missing permission.
    """

    def __init__(self, token: str | None = None, channel_id: str | None = None) -> None:
        self.token = (token if token is not None else config.DISCORD_TOKEN).strip()
        self.channel_id = (
            channel_id if channel_id is not None else config.DISCORD_CHANNEL_ID
        ).strip()
        self._client = httpx.Client(timeout=config.HTTP_TIMEOUT)
        self._last_send = 0.0
        # HTTP status of the most recent failed send (None for a transport
        # error or a success). Lets send_alert tell "Discord rejected this
        # body" from "Discord is unreachable" without _post's bool growing.
        self.last_status: int | None = None

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "Discord":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_send
        if elapsed < config.DISCORD_RATE_DELAY:
            time.sleep(config.DISCORD_RATE_DELAY - elapsed)
        self._last_send = time.monotonic()

    def _configured(self) -> bool:
        if not self.token or not self.channel_id:
            log.error("Discord is not configured: set DISCORD_TOKEN and DISCORD_CHANNEL_ID")
            return False
        if not self.channel_id.isdigit():
            # The classic mistake: a channel *name*, or a pasted URL, where the
            # numeric id (Developer Mode -> right-click -> Copy Channel ID) goes.
            log.error("DISCORD_CHANNEL_ID must be the numeric channel id, got %r", self.channel_id)
            return False
        return True

    @staticmethod
    def _retry_after(resp: httpx.Response) -> float | None:
        try:
            return float(resp.json().get("retry_after"))
        except (ValueError, TypeError, AttributeError):
            return None

    def _post(self, embed: dict) -> bool:
        self.last_status = None
        if not self._configured():
            return False
        payload = {
            "embeds": [embed],
            # Seller text is untrusted: a listing titled "@everyone" or carrying
            # "<@123>" must be text, never a ping. Embeds do not notify, but
            # this closes the door regardless of where the text ends up.
            "allowed_mentions": {"parse": []},
        }
        url = f"{API}/channels/{self.channel_id}/messages"
        headers = {"Authorization": f"Bot {self.token}"}
        for attempt in range(1, SEND_ATTEMPTS + 1):
            try:
                resp = self._client.post(url, json=payload, headers=headers)
            except httpx.HTTPError as exc:
                log.error("Discord send failed: %s", exc)
                return False
            if resp.status_code in (200, 201):
                return True
            self.last_status = resp.status_code
            if resp.status_code == 429 and attempt < SEND_ATTEMPTS:
                wait = self._retry_after(resp)
                if wait is not None and wait <= MAX_RETRY_WAIT_SECONDS:
                    log.warning("Discord rate limited, retrying in %.1fs", wait)
                    time.sleep(wait)
                    continue
            log.error("Discord -> %s: %s", resp.status_code, resp.text[:300])
            return False
        return False

    @staticmethod
    def _embed(title: str, body: str, color: int) -> dict:
        return {
            "title": title[:EMBED_TITLE_LIMIT],
            "description": body[:EMBED_DESCRIPTION_LIMIT],
            "color": color,
        }

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
        title, body = split_title(caption)

        # The caption ends with the listing URL. The embed title links to it, so
        # repeating it as a bare line underneath is clutter.
        if item.web_url:
            tail = html.escape(item.web_url)
            body = body.rstrip()
            if body.endswith(tail):
                body = body[: -len(tail)].rstrip()

        if kind == "price_drop":
            color = COLOR_DROP
        elif deal.priced:
            color = COLOR_DEAL
        else:
            color = COLOR_MATCH

        embed = self._embed(title, html_to_markdown(body), color)
        if item.web_url:
            embed["url"] = item.web_url
        if item.image_url:
            embed["image"] = {"url": item.image_url}

        sent = self._post(embed)
        if not sent and self.last_status == 400 and "image" in embed:
            # A malformed image URL is not "a picture that fails to load" —
            # Discord drops those silently — it is a 400 on the whole body
            # ("Not a well formed URL"), and that loses the alert. Found by
            # sending one, not by reading the docs. Retry once without the
            # picture; only on a 400, so an outage does not double its requests.
            log.warning("Discord rejected the embed; retrying %s without the image", item.item_id)
            del embed["image"]
            sent = self._post(embed)
        return sent

    def send_error(self, text: str) -> bool:
        """Surface failures instead of dying silently in a cron runner.

        Returns whether the ping was actually delivered. It used to return
        nothing, which let a failed send look identical to a successful one —
        and the callers that start a cooldown on "we told the owner" were
        starting it on a message nobody received.
        """
        if config.DRY_RUN:
            log.info("[DRY RUN] error ping: %s", text)
            return True
        self._throttle()
        title, body = split_title(build_error_text(text))
        return self._post(self._embed(title, html_to_markdown(body), COLOR_ERROR))
