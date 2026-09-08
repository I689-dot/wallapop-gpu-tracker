"""Thin, defensive client for Wallapop's internal search API.

The v3 search endpoint changes shape between releases, so both the envelope
(where the item array lives) and the item fields are read through fallback
chains rather than fixed paths. If a fetch starts returning 403, open
wallapop.es with DevTools -> Network, copy the headers off a real
`search?...` request, and update HEADERS below.
"""

from __future__ import annotations

import logging
import math
import random
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

import httpx

import config

log = logging.getLogger("wallapop")

SEARCH_URL = "https://api.wallapop.com/api/v3/search"
ITEM_DETAIL_URL = "https://api.wallapop.com/api/v3/items/{item_id}"

# Verified against the live API 2026-08-01: `source` is mandatory — without it
# every request is a bare 400 regardless of the other params.
REQUIRED_PARAMS = {"source": "search_box"}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "X-DeviceOS": "0",
    "Origin": "https://es.wallapop.com",
    "Referer": "https://es.wallapop.com/",
}


@dataclass
class Item:
    item_id: str
    title: str
    description: str
    price: float | None
    currency: str
    web_url: str
    image_url: str | None
    reserved: bool
    shipping: bool
    location: str | None
    distance_km: float | None
    country: str | None = None
    seller_id: str | None = None
    condition: str | None = None
    brand: str | None = None
    taxonomy: tuple[int, ...] = ()
    posted_at: datetime | None = None
    modified_at: datetime | None = None
    user_allows_shipping: bool | None = None

    @property
    def status(self) -> str:
        return "reserved" if self.reserved else "active"

    @property
    def can_ship(self) -> bool:
        """Whether the *seller* actually enabled shipping on this listing.

        `shipping` (item_is_shippable) is a category capability — "GPUs are a
        shippable kind of thing" — not a seller's choice. Live listings exist
        with item_is_shippable=true and user_allows_shipping=false, so prefer
        the seller flag when we have it and only fall back to the category
        flag when the API didn't return it (e.g. some search-response shapes).
        """
        return self.user_allows_shipping if self.user_allows_shipping is not None else self.shipping

    @property
    def whole_machine(self) -> bool:
        """Is this a built machine rather than a loose part?

        The *terminal* node decides, not any node on the path. `any(...)` over
        the whole taxonomy was the original test and it made a parent answer on
        behalf of its children: "PC gaming y streaming" (24115) is a branch, and
        every listing beneath it carries 24115 in its path — including the
        loose-card leaf 24130. Nine confirmed graphics-card sales were marked as
        whole machines that way ("Sapphire Pulse RX 9060XT 16GB", "MSI RTX 3090
        24GB", "Zotac RTX 4060", three of one 9070 XT), each one then barred
        from its own comps pool by db.sold_comps with nothing logged to say so.

        The deepest node is what the seller actually chose, so it is the one
        that answers. A path that stops at 24115 is still a whole machine — the
        seller filed it under gaming PCs and went no further — which is why that
        id stays in the set.
        """
        if not self.taxonomy:
            return False
        return self.taxonomy[-1] in config.TAXONOMY_WHOLE_MACHINE

    @property
    def age_seconds(self) -> float | None:
        if self.posted_at is None:
            return None
        return (datetime.now(timezone.utc) - self.posted_at).total_seconds()


# --------------------------------------------------------------- extraction
def _first(d: Any, *paths: str, default: Any = None) -> Any:
    """Read the first path that resolves. Paths are dotted, e.g. 'price.amount'."""
    for path in paths:
        cur = d
        ok = True
        for part in path.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                ok = False
                break
        if ok and cur not in (None, ""):
            return cur
    return default


def extract_items(payload: Any) -> list[dict]:
    """Find the item array wherever this API version decided to put it."""
    if not isinstance(payload, dict):
        return []
    candidates = (
        "search_objects",
        "items",
        "data.section.payload.items",
        "data.items",
        "section.payload.items",
        "payload.items",
    )
    for path in candidates:
        found = _first(payload, path)
        if isinstance(found, list) and found:
            return [x for x in found if isinstance(x, dict)]
    # Last resort: some versions wrap each hit as {"type":..,"content":{...}}
    for value in payload.values():
        if isinstance(value, list) and value and isinstance(value[0], dict):
            if any(k in value[0] for k in ("id", "item_id", "content")):
                return value
    return []


def _unwrap(raw: dict) -> dict:
    """Some shapes nest the real item under 'content' or 'item'."""
    for key in ("content", "item"):
        inner = raw.get(key)
        if isinstance(inner, dict) and ("id" in inner or "title" in inner):
            return inner
    return raw


def _price(raw: dict) -> tuple[float | None, str]:
    # "price.cash.amount"/"price.cash.currency" is the /items/{id} detail
    # shape; "price.amount"/"price.currency" is the /search shape. Detail
    # paths go first — order matters in _first, since "price" itself would
    # resolve to a non-empty dict on the detail shape and short-circuit
    # before the more specific path is even tried.
    value = _first(
        raw, "price.cash.amount", "price.amount", "price", "sale_price", "price_amount"
    )
    currency = _first(raw, "price.cash.currency", "price.currency", "currency", default="EUR")
    if isinstance(value, dict):  # nested one level deeper in some versions
        currency = value.get("currency", currency)
        value = value.get("amount")
    try:
        return (float(value), str(currency)) if value is not None else (None, str(currency))
    except (TypeError, ValueError):
        return None, str(currency)


def _image(raw: dict) -> str | None:
    images = raw.get("images") or raw.get("image") or []
    if isinstance(images, dict):
        images = [images]
    if isinstance(images, list):
        for img in images:
            if isinstance(img, str):
                return img
            if isinstance(img, dict):
                url = _first(
                    img,
                    "urls.big",
                    "urls.medium",
                    "urls.original",
                    "urls.small",
                    "original",
                    "big",
                    "medium",
                    "url",
                )
                if url:
                    return str(url)
    return _first(raw, "main_image.urls.big", "main_image.urls.medium", "main_image.url")


def _flag(raw: dict, *paths: str) -> bool:
    """Read a boolean that the API may wrap as {"flag": true}.

    The live shape is `"reserved": {"flag": false}` — reading the bare key would
    make every listing truthy, so the wrapped path is tried first.
    """
    for path in paths:
        val = _first(raw, path)
        if isinstance(val, dict):
            val = val.get("flag")
        if isinstance(val, bool):
            return val
        if val is not None:
            return bool(val)
    return False


def _reserved(raw: dict) -> bool:
    return _flag(raw, "reserved.flag", "reserved", "flags.reserved", "is_reserved")


def _shipping(raw: dict) -> bool:
    """Category-level shippability ("GPUs are the kind of thing Wallapop lets
    ship"), NOT whether this particular seller enabled it. Kept as-is because
    other code depends on this exact field; see `_user_allows_shipping` below
    and `Item.can_ship` for the flag that actually reflects the seller.
    """
    return _flag(
        raw,
        "shipping.item_is_shippable",
        "shipping_allowed",
        "flags.shipping_allowed",
    )


def _user_allows_shipping(raw: dict) -> bool | None:
    """Whether the seller enabled shipping on this specific listing.

    Verified live: `item_is_shippable: true` and `user_allows_shipping: false`
    coexist on real listings, so the two must not be conflated. Distinct from
    `_flag` in that a missing field must stay None here (unknown), not
    collapse to False the way `_flag`'s callers are happy to accept.
    """
    val = _first(raw, "shipping.user_allows_shipping")
    if isinstance(val, dict):
        val = val.get("flag")
    if val is None:
        return None
    return bool(val)


def _seller_id(raw: dict) -> str | None:
    """The account behind the listing.

    Worth having for one reason: it is the only identifier that survives a
    relisting. The same replica, empty box or "GPU" that is really a cooler
    reappears under a fresh item_id every few days, so the (item_id, price)
    alert dedup never sees it twice and the same seller's junk is alerted
    indefinitely. config.BLOCKED_SELLERS is applied against this.

    The two endpoints disagree about where it lives — /search nests it under the
    `user` object while /items/{id} has carried a flat `user_id` — hence the
    fallback chain, same as everything else in this file. A dict or list result
    means this version nests it somewhere new; return None rather than persist
    the repr of a container as if it were an id.
    """
    value = _first(
        raw,
        "user.id",
        "user.user_id",
        "user.hash",
        "user.uuid",
        "user_id",
        "seller_id",
        "seller.id",
        "owner.id",
        "owner_id",
    )
    if value is None or isinstance(value, (dict, list)):
        return None
    return str(value)


def _taxonomy(raw: dict) -> tuple[int, ...]:
    """Category ids the listing is filed under.

    The id is an int on the /search response but a string on the /items/{id}
    detail response — coerce both and drop anything that isn't numeric rather
    than let one bad node blow up the whole item.
    """
    nodes = raw.get("taxonomy")
    if not isinstance(nodes, list):
        return ()
    ids: list[int] = []
    for node in nodes:
        if not isinstance(node, dict):
            continue
        try:
            ids.append(int(node.get("id")))
        except (TypeError, ValueError):
            continue
    return tuple(ids)


def _epoch_ms(raw: dict, *paths: str) -> datetime | None:
    """Parse an epoch-milliseconds timestamp field (`created_at`/`modified_at`).

    Bounds-checked against a sane range so a stray unit mixup (seconds
    instead of ms) or a garbage value produces None instead of a nonsense
    datetime decades away — never let this raise.
    """
    value = _first(raw, *paths)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        dt = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    except (ValueError, OSError, OverflowError):
        return None
    if dt.year < 2015 or dt > datetime.now(timezone.utc) + timedelta(days=1):
        return None
    return dt


def _backoff_seconds(attempt: int) -> float:
    """Seconds to wait before retrying attempt `attempt` (1-based).

    Exponential, with +/-25% of jitter. The jitter is not cosmetic: this client
    talks to a rate-limiting API from CI runners, several of which start on the
    same cron minute. A deterministic 2/4/8s backoff means every runner that got
    a 429 retries at exactly the same instant as the others, re-creating the
    burst that caused the 429 in the first place; spreading them out is what
    lets a retry actually land.
    """
    base = float(2 ** attempt)
    return base * random.uniform(0.75, 1.25)


def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in km."""
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _distance_km(raw: dict) -> float | None:
    """Distance from the configured centre.

    The API doesn't return one, but every listing carries its own coordinates,
    so it's computed locally rather than left blank in the alert.
    """
    direct = _first(raw, "distance", "location.distance")
    if isinstance(direct, (int, float)):
        return float(direct)
    lat = _first(raw, "location.latitude")
    lon = _first(raw, "location.longitude")
    if isinstance(lat, (int, float)) and isinstance(lon, (int, float)):
        return round(_haversine(config.LATITUDE, config.LONGITUDE, float(lat), float(lon)), 1)
    return None


def parse_item(raw: dict) -> Item | None:
    raw = _unwrap(raw)
    item_id = _first(raw, "id", "item_id", "item_uuid")
    if not item_id:
        return None
    slug = _first(raw, "web_slug", "slug", "share_url")
    if slug and str(slug).startswith("http"):
        web_url = str(slug)
    elif slug:
        web_url = f"https://es.wallapop.com/item/{slug}"
    else:
        web_url = f"https://es.wallapop.com/item/{item_id}"
    price, currency = _price(raw)
    # "title.original"/"description.original" is the /items/{id} detail
    # shape (plain strings on /search); detail path goes first for the same
    # short-circuit reason as _price above.
    return Item(
        item_id=str(item_id),
        title=str(_first(raw, "title.original", "title", "name", default="") or ""),
        description=str(
            _first(raw, "description.original", "description", "body", default="") or ""
        ),
        price=price,
        currency=currency,
        web_url=web_url,
        image_url=_image(raw),
        reserved=_reserved(raw),
        shipping=_shipping(raw),
        location=_first(raw, "location.city", "location.name", "user.location.city"),
        distance_km=_distance_km(raw),
        country=_first(raw, "location.country_code", "user.location.country_code"),
        seller_id=_seller_id(raw),
        # condition/brand live under type_attributes on the detail endpoint;
        # /search doesn't return them, so this is None on search results.
        condition=_first(raw, "type_attributes.condition.value"),
        brand=_first(raw, "type_attributes.brand.value"),
        taxonomy=_taxonomy(raw),
        posted_at=_epoch_ms(raw, "created_at"),
        modified_at=_epoch_ms(raw, "modified_at"),
        user_allows_shipping=_user_allows_shipping(raw),
    )


# --------------------------------------------------------------- the client
class WallapopClient:
    def __init__(self, client: httpx.Client | None = None) -> None:
        self._client = client or httpx.Client(
            headers=HEADERS, timeout=config.HTTP_TIMEOUT, follow_redirects=True
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "WallapopClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # Rate-limited/blocked and transient server errors are worth a backoff
    # retry; any other status (400, 404, ...) means the request itself is
    # wrong and retrying it changes nothing.
    _RETRYABLE_STATUS = {403, 429, 500, 502, 503, 504}

    def _request(self, url: str, params: dict | None = None) -> httpx.Response | None:
        """One GET with backoff, returning the raw response.

        Shared by `_get` (search) and `fetch_detail`/`is_alive` (item detail).
        Returns the response object rather than parsed JSON so callers that
        need the status code — `is_alive` in particular, which must tell a
        confirmed 404 apart from a network blip — don't lose it. A 404 is
        returned like a 200 (not retried, not swallowed): it's a wrong-request
        story the same way 400 is, but callers here need to see it rather than
        have it collapse to None.

        Nothing sleeps after the final attempt. Both branches used to back off
        unconditionally, so a fully-exhausted request burned 2+4+8s and returned
        None having spent its last 8 seconds waiting for a retry that was never
        going to happen — 8s per dead request, on a loop that issues one per
        search plus one per liveness check.
        """
        for attempt in range(1, config.HTTP_RETRIES + 1):
            final = attempt == config.HTTP_RETRIES
            try:
                resp = self._client.get(url, params=params)
                if resp.status_code == 200 or resp.status_code == 404:
                    return resp
                if resp.status_code in self._RETRYABLE_STATUS:
                    if final:
                        log.warning(
                            "HTTP %s from Wallapop on the last of %d attempts — giving up",
                            resp.status_code, config.HTTP_RETRIES,
                        )
                        return None
                    wait = _backoff_seconds(attempt)
                    log.warning(
                        "HTTP %s from Wallapop (attempt %d/%d) — backing off %.1fs",
                        resp.status_code, attempt, config.HTTP_RETRIES, wait,
                    )
                    time.sleep(wait)
                    continue
                log.warning("HTTP %s from Wallapop: %s", resp.status_code, resp.text[:200])
                return None
            except (httpx.HTTPError, ValueError) as exc:
                log.warning("Request failed (attempt %d/%d): %s", attempt, config.HTTP_RETRIES, exc)
                if final:
                    break
                time.sleep(_backoff_seconds(attempt))
        return None

    def _get(self, params: dict) -> dict | None:
        """Search request. Returns the parsed payload, or None on any failure
        (a 404 included — the search endpoint never legitimately returns one).
        """
        resp = self._request(SEARCH_URL, params)
        if resp is None or resp.status_code != 200:
            return None
        try:
            return resp.json()
        except ValueError as exc:
            log.warning("Malformed JSON from Wallapop: %s", exc)
            return None

    def fetch_detail(self, item_id: str) -> Item | None:
        """GET /items/{id} and parse it. None on any failure, 404 included —
        call `is_alive` first if the caller needs to know *why* it failed.
        """
        resp = self._request(ITEM_DETAIL_URL.format(item_id=item_id))
        if resp is None or resp.status_code != 200:
            return None
        try:
            payload = resp.json()
        except ValueError as exc:
            log.warning("Malformed JSON from Wallapop item detail: %s", exc)
            return None
        if not isinstance(payload, dict):
            return None
        return parse_item(payload)

    def is_alive(self, item_id: str) -> bool | None:
        """Whether the listing still exists on Wallapop.

        False only on a confirmed 404. Any other failure (timeout, 5xx,
        malformed response) returns None rather than False — a request we
        couldn't complete is not evidence the item sold, and sale inference
        must never treat "we couldn't tell" as "it's gone."
        """
        resp = self._request(ITEM_DETAIL_URL.format(item_id=item_id))
        if resp is None:
            return None
        if resp.status_code == 404:
            return False
        if resp.status_code == 200:
            return True
        return None

    def search(
        self,
        keywords: str,
        *,
        min_price: float | None = None,
        max_price: float | None = None,
        category_ids: str | None = None,
        distance_km: int | None = None,
        order_by: str = "newest",
        max_pages: int = 1,
        nationwide: bool = False,
        time_filter: str | None = None,
        condition: str | None = None,
    ) -> Iterator[Item]:
        """Yield parsed items, following pagination up to max_pages.

        `nationwide=True` drops the geo filter entirely. Wallapop runs one
        shared marketplace across Spain, Portugal, Italy (and more) rather than
        per-country endpoints — there's no country_code/country param that
        actually restricts results (verified live: both are silently ignored),
        so dropping lat/lon/distance is the only way to see the full
        cross-border listing pool. Both loops use this now: comps needs the
        widest price distribution, and alerts want every country's deals.
        """
        base: dict[str, Any] = {**REQUIRED_PARAMS, "keywords": keywords, "order_by": order_by}
        if not nationwide:
            base["latitude"] = config.LATITUDE
            base["longitude"] = config.LONGITUDE
            base["distance_in_km"] = distance_km or config.DEFAULT_DISTANCE_KM
        if min_price is not None:
            base["min_sale_price"] = int(min_price)
        if max_price is not None:
            base["max_sale_price"] = int(max_price)
        if category_ids:
            base["category_ids"] = category_ids
        if time_filter:
            # Verified live 2026-08-02: any valid time_filter value ("today" /
            # "lastWeek" / "lastMonth") raises the API's page size from 16 to
            # 40 items — no other param does this (limit, items_count, and
            # filters_source were all tried as controls and had zero effect).
            # An invalid value is a straight HTTP 400, so only send it when set.
            base["time_filter"] = time_filter
        if condition:
            base["condition"] = condition

        seen: set[str] = set()
        next_token: str | None = None
        offset = 0

        for page in range(max_pages):
            params = dict(base)
            if next_token:
                params["next_page"] = next_token
            elif offset:
                params["start"] = offset

            payload = self._get(params)
            if payload is None:
                return

            raws = extract_items(payload)
            if not raws:
                if page == 0:
                    # "response keys" alone was not enough to diagnose this the
                    # first time: a geolocated-empty result and a genuine schema
                    # change both look like {'data','meta','stats'}. The section
                    # type distinguishes them — a well-formed but empty
                    # organic_search_results means the query matched nothing
                    # where the server thinks we are, not that parsing broke.
                    section = _first(payload, "data.section") or {}
                    log.warning(
                        "No items parsed for %r — keys=%s section_type=%s "
                        "params=%s. A well-formed empty section usually means "
                        "the request geolocated outside the marketplace; check "
                        "that a time_filter is being sent.",
                        keywords,
                        list(payload)[:8] if isinstance(payload, dict) else type(payload),
                        section.get("type") if isinstance(section, dict) else None,
                        {k: v for k, v in params.items() if k != "next_page"},
                    )
                return

            emitted = 0
            for raw in raws:
                item = parse_item(raw)
                if item is None or item.item_id in seen:
                    continue
                seen.add(item.item_id)
                emitted += 1
                yield item

            next_token = _first(payload, "meta.next_page", "next_page", "meta.cursor")
            offset += len(raws)
            if not next_token and emitted == 0:
                return
            if page + 1 < max_pages:
                time.sleep(config.REQUEST_DELAY)
