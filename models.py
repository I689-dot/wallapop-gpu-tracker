"""GPU title -> canonical model_key parser.

GPUs are the only product family this parser knows about, and deliberately so:
phone/iPhone tracking was tried and reverted, and the owner does not want other
families back. Anything that used to exist here to keep families apart has been
removed rather than left dormant.

Comps are only meaningful if a "4070" is never pooled with a "4070 Ti Super",
so every listing is normalised to a canonical key before it can be priced.

Rules:
  * most-specific pattern wins (ti super > ti > super > base), so the registry
    is ordered and the first match returns;
  * VRAM disambiguates the split SKUs (4060 Ti / 5060 Ti 8GB vs 16GB); the
    figure used is the one written *nearest the model number*, not the largest
    one in the string (see extract_vram); when the text doesn't say, it falls
    back to a separate generic key rather than guessing into one of the two
    pools;
  * the title is tried first and wins outright. Only if the title matches
    nothing at all is the description scanned, and such a match is capped at
    'medium' confidence — a description is far noisier than a title, so it may
    price a listing but must never outrank or override what the title says. A
    description naming more than one distinct card is capped at 'low' instead:
    a parts list or a bundle blurb gives no way to tell which card is for sale,
    and guessing systematically invents bargains (see classify);
  * confidence is 'high' only when a brand token (rtx/geforce/radeon/rx/...)
    backs up the number. A bare "4070" in a noisy title returns 'low', and the
    caller must not price a low-confidence match.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, replace


# --------------------------------------------------------------- normalising
def normalise(text: str | None) -> str:
    """Lowercase, strip accents, collapse punctuation into single spaces.

    Also splits letter/digit runs ("4060ti" -> "4060 ti", "8gb" -> "8 gb") so a
    single spaced pattern matches every way people write these names.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"(\d)([a-z])", r"\1 \2", text)
    text = re.sub(r"([a-z])(\d)", r"\1 \2", text)
    return re.sub(r"\s+", " ", text).strip()


# "amd" stays in, even though AMD also makes Ryzen CPUs whose model numbers
# can collide with Radeon GPU numbers (Ryzen 5 7600 vs RX 7600) — junk.py's
# CPU_TOKENS filter is the line of defense for that case. Dropping "amd" here
# would cost real AMD GPU listings that don't literally say "rx"/"radeon".
BRAND_TOKENS = (
    "rtx", "gtx", "nvidia", "geforce", "radeon", "rx", "amd", "gpu",
    "grafica", "graficas", "tarjeta grafica", "vga",
)

# Brand tokens that belong to one vendor, used to stop a *rival's* branding
# from validating a match. Bare model numbers collide across vendors — AMD's
# "RX 7600" is 2023, NVIDIA's "GeForce 7600 GS" is 2006 — and without this a
# listing reading "PC de escritorio HP P4 + Nvidia 7600GS" (a real listing, at
# 60 EUR) classifies as an RX 7600, prices against a 200 EUR reference, and
# reports a 143 EUR margin on a twenty-year-old card.
APPLE_TOKENS = ("iphone", "apple")

AMD_TOKENS = ("rx", "radeon", "amd")
NVIDIA_TOKENS = ("rtx", "gtx", "nvidia", "geforce")

VRAM_RE = re.compile(r"\b(4|6|8|10|11|12|16|20|24|32)\s*(?:gb|g|gigas?)\b")

@dataclass(frozen=True)
class ModelDef:
    key: str
    display: str
    pattern: str
    # When set, the match only counts if the text's VRAM equals this value.
    vram: int | None = None
    # Which product this is. Removed once as dead scaffolding when phone
    # tracking was reverted; it is load-bearing again now that iPhones are
    # tracked for comps, because family decides which junk rules apply, how
    # confidence is judged, and — critically — whether a match may ever alert.
    # Only 'gpu' alerts. See alert_loop.ALERTING_FAMILIES.
    family: str = "gpu"


# AIB product-line words a manufacturer SKU glues straight onto the model
# suffix with no separator. normalise() splits letter/digit runs but not
# letter/letter ones, so "GV-R9070XTGAMING OC-16GD" arrives as
# "gv r 9070 xtgaming oc 16 gd": the "xt" is there, but the \b that used to
# close the RX 9070 XT pattern falls between "t" and "g" and never fires. The
# entry below it, plain RX 9070, has no suffix to lose and matched instead —
# three confirmed sales of a 9070 XT were filed as an RX 9070 at 607.99 EUR,
# inflating one pool and starving the other.
#
# Spelled out rather than left as a bare `[a-z]*`, which would have "5070 ti"
# swallow the extremely ordinary Spanish word "tienda" and promote every plain
# 5070 to a Ti.
_GLUED_AIB_LINES = (
    "gaming", "oc", "eagle", "aorus", "pulse", "nitro", "windforce", "ventus",
    "gamerock", "phantom", "hellhound", "challenger", "steellegend", "taichi",
    "pure", "trinity", "twin", "solid", "swift", "xtreme", "master", "elite",
)

# How a model suffix ends: a word boundary, or the start of one of the AIB
# line words above.
_SUFFIX_END = r"(?:\b|(?=(?:%s)\b))" % "|".join(_GLUED_AIB_LINES)

# Suffixes that separate a specific card from its base model. A base entry must
# refuse a number carrying one of these, so that a suffix the specific pattern
# somehow failed to read cannot fall through and be priced a tier too low.
_MODEL_SUFFIXES = ("xtx", "xt", "ti", "super", "gre")
_NO_SUFFIX_AHEAD = r"(?!\s*(?:%s)%s)" % ("|".join(_MODEL_SUFFIXES), _SUFFIX_END)


def _num(n: str, *suffix: str) -> str:
    """Pattern for a GPU number plus optional suffix words.

    Allows an optional leading brand token and tolerates the separators people
    actually type ("rtx4070ti", "RTX 4070 TI", "4070-Ti", "GV-R9070XTGAMING").

    With no suffix this builds a *base* pattern, which additionally refuses any
    number followed by a model suffix — see _NO_SUFFIX_AHEAD.
    """
    if not suffix:
        return rf"\b{n}\b" + _NO_SUFFIX_AHEAD
    parts = [rf"\b{n}"]
    for s in suffix:
        parts.append(rf"\s+{s}")
    return "".join(parts) + _SUFFIX_END


def _iph(n: str, *suffix: str) -> str:
    """Pattern for an iPhone model number plus optional suffix words.

    Requires the literal word "iphone" before the number. A bare "15 pro" is far
    too generic to risk — unlike a GPU part number, which is distinctive on its
    own, two-digit phone numbers collide with sizes, quantities and years all
    over a marketplace listing.
    """
    parts = [r"\biphone\s+" + n]
    for suf in suffix:
        parts.append(rf"\s+{suf}")
    return "".join(parts) + r"\b"


# Console name as normalise() leaves it: "PS5" arrives as "ps 5", "PlayStation
# 5" unchanged, "Xbox Series X" unchanged. Sellers write all of these, plus
# "Play 5" and the singular "serie x".
_PS5 = r"(?:\bps\s*5\b|\bplay\s*(?:station\s*)?5\b)"
_XSX = r"\bxbox\s+seri[ea]s?\s+x\b"

# Nouns that open a genuine console listing, shared with junk.py (which imports
# them from here — the dependency runs junk -> models, never the other way).
#
# This is the whole basis of console classification, and it is an observation
# about how the two kinds of listing are written rather than a heuristic: a
# console leads with what it is ("Consola PS5 con mando", "Xbox Series X 1TB
# SSD con 2 mandos"), while a game leads with the game and names the platform
# afterwards, because the platform is what buyers search for ("Elden Ring PS5",
# "Hogwarts Legacy Xbox Series X"). Sampled live on 2026-08-26, this separated
# 17 consoles from 25 games and accessories with no errors either way.
CONSOLE_LEAD_NOUNS = (
    "consola", "consolas",
    "ps 5", "playstation 5", "play station 5", "play 5",
    "xbox series x", "xbox serie x", "xbox series s", "xbox serie s", "xbox",
)


def _console_titled(norm_title: str) -> bool:
    """True when the title opens by naming the console itself."""
    head = " ".join(norm_title.split()[:3])
    return any(head.startswith(noun) for noun in CONSOLE_LEAD_NOUNS)


# Ordered most-specific first. First match wins.
REGISTRY: tuple[ModelDef, ...] = (
    # ---------------------------------------------------------- RTX 50 series
    ModelDef("rtx_5080", "RTX 5080", _num("5080")),
    ModelDef("rtx_5070_ti", "RTX 5070 Ti", _num("5070", "ti")),
    ModelDef("rtx_5070", "RTX 5070", _num("5070")),
    ModelDef("rtx_5060_ti_16g", "RTX 5060 Ti 16GB", _num("5060", "ti"), vram=16),
    ModelDef("rtx_5060_ti_8g", "RTX 5060 Ti 8GB", _num("5060", "ti"), vram=8),
    ModelDef("rtx_5060_ti", "RTX 5060 Ti", _num("5060", "ti")),
    ModelDef("rtx_5060", "RTX 5060", _num("5060")),
    # ---------------------------------------------------------- RTX 40 series
    ModelDef("rtx_4080_super", "RTX 4080 Super", _num("4080", "super")),
    ModelDef("rtx_4080", "RTX 4080", _num("4080")),
    ModelDef("rtx_4070_ti_super", "RTX 4070 Ti Super", _num("4070", "ti", "super")),
    ModelDef("rtx_4070_ti", "RTX 4070 Ti", _num("4070", "ti")),
    ModelDef("rtx_4070_super", "RTX 4070 Super", _num("4070", "super")),
    ModelDef("rtx_4070", "RTX 4070", _num("4070")),
    ModelDef("rtx_4060_ti_16g", "RTX 4060 Ti 16GB", _num("4060", "ti"), vram=16),
    ModelDef("rtx_4060_ti_8g", "RTX 4060 Ti 8GB", _num("4060", "ti"), vram=8),
    ModelDef("rtx_4060_ti", "RTX 4060 Ti", _num("4060", "ti")),
    ModelDef("rtx_4060", "RTX 4060", _num("4060")),
    # ---------------------------------------------------------- RTX 30 series
    ModelDef("rtx_3090_ti", "RTX 3090 Ti", _num("3090", "ti")),
    ModelDef("rtx_3090", "RTX 3090", _num("3090")),
    ModelDef("rtx_3080_ti", "RTX 3080 Ti", _num("3080", "ti")),
    ModelDef("rtx_3080_12g", "RTX 3080 12GB", _num("3080"), vram=12),
    ModelDef("rtx_3080", "RTX 3080", _num("3080")),
    ModelDef("rtx_3070_ti", "RTX 3070 Ti", _num("3070", "ti")),
    ModelDef("rtx_3070", "RTX 3070", _num("3070")),
    ModelDef("rtx_3060_ti", "RTX 3060 Ti", _num("3060", "ti")),
    ModelDef("rtx_3060_12g", "RTX 3060 12GB", _num("3060"), vram=12),
    ModelDef("rtx_3060", "RTX 3060", _num("3060")),
    ModelDef("rtx_3050", "RTX 3050", _num("3050")),
    # ------------------------------------------------------------ AMD RX 9000
    ModelDef("rx_9070_xt", "RX 9070 XT", _num("9070", "xt")),
    ModelDef("rx_9070", "RX 9070", _num("9070")),
    ModelDef("rx_9060_xt_16g", "RX 9060 XT 16GB", _num("9060", "xt"), vram=16),
    ModelDef("rx_9060_xt_8g", "RX 9060 XT 8GB", _num("9060", "xt"), vram=8),
    ModelDef("rx_9060_xt", "RX 9060 XT", _num("9060", "xt")),
    # ------------------------------------------------------------ AMD RX 7000
    ModelDef("rx_7900_xtx", "RX 7900 XTX", _num("7900", "xtx")),
    ModelDef("rx_7900_xt", "RX 7900 XT", _num("7900", "xt")),
    ModelDef("rx_7900_gre", "RX 7900 GRE", _num("7900", "gre")),
    ModelDef("rx_7800_xt", "RX 7800 XT", _num("7800", "xt")),
    ModelDef("rx_7700_xt", "RX 7700 XT", _num("7700", "xt")),
    ModelDef("rx_7600_xt", "RX 7600 XT", _num("7600", "xt")),
    ModelDef("rx_7600", "RX 7600", _num("7600")),
    # ------------------------------------------------------------ AMD RX 6000
    ModelDef("rx_6800_xt", "RX 6800 XT", _num("6800", "xt")),
    ModelDef("rx_6800", "RX 6800", _num("6800")),
    ModelDef("rx_6750_xt", "RX 6750 XT", _num("6750", "xt")),
    ModelDef("rx_6700_xt", "RX 6700 XT", _num("6700", "xt")),
    ModelDef("rx_6650_xt", "RX 6650 XT", _num("6650", "xt")),
    ModelDef("rx_6600_xt", "RX 6600 XT", _num("6600", "xt")),
    ModelDef("rx_6600", "RX 6600", _num("6600")),
    # ------------------------------------------------------------ iPhone
    # Comps-only: tracked to learn what these actually sell for. Nothing here
    # can alert — see alert_loop.ALERTING_FAMILIES.
    #
    # Ordered most-specific first like everything above, so "pro max" is tested
    # before "pro" and "pro" before the bare number. normalise() has already
    # split letter/digit runs, so "iPhone15Pro" arrives as "iphone 15 pro" and
    # one spaced pattern matches every way people write these.
    #
    # `\s+` between tokens rather than optional separators because normalise()
    # collapses everything to single spaces.
    # ------------------------------------------------------------- consoles
    # Comps only, like the phones: tracked so the bot learns what they resell
    # for, never alerted on (alert_loop.ALERTING_FAMILIES).
    #
    # Split by SKU because the price spread within "a PS5" is far too wide to
    # pool — a Pro and a Digital are a couple of hundred euros apart, and a
    # median over both describes neither. Ordered most-specific first, so a
    # "PS5 Pro" never falls through to the base model.
    ModelDef("ps5_pro", "PS5 Pro", _PS5 + r"\s+pro\b", family="console"),
    # Up to two words may sit between the name and "digital", because sellers
    # write "PS5 Slim Edicion Digital" as often as "PS5 Digital".
    ModelDef("ps5_digital", "PS5 Digital",
             _PS5 + r"\s+(?:\w+\s+){0,2}?(?:edicion\s+)?digital\b", family="console"),
    ModelDef("ps5", "PS5", _PS5, family="console"),
    # Requires "series"/"serie" so "Xbox One X" can never match, and ends on a
    # word boundary so "Xbox Series S" cannot either.
    ModelDef("xbox_series_x", "Xbox Series X", _XSX, family="console"),
    ModelDef("iphone_15_pro_max", "iPhone 15 Pro Max", _iph("15", "pro", "max"), family="phone"),
    ModelDef("iphone_15_pro", "iPhone 15 Pro", _iph("15", "pro"), family="phone"),
    ModelDef("iphone_15_plus", "iPhone 15 Plus", _iph("15", "plus"), family="phone"),
    ModelDef("iphone_15", "iPhone 15", _iph("15"), family="phone"),
    ModelDef("iphone_16_pro_max", "iPhone 16 Pro Max", _iph("16", "pro", "max"), family="phone"),
    ModelDef("iphone_16_pro", "iPhone 16 Pro", _iph("16", "pro"), family="phone"),
    ModelDef("iphone_16_plus", "iPhone 16 Plus", _iph("16", "plus"), family="phone"),
    # 16e before 16: normalise() splits "16e" into "16 e", so the bare-16
    # pattern would otherwise swallow it.
    ModelDef("iphone_16e", "iPhone 16e", _iph("16", "e"), family="phone"),
    ModelDef("iphone_16", "iPhone 16", _iph("16"), family="phone"),
    ModelDef("iphone_17_pro_max", "iPhone 17 Pro Max", _iph("17", "pro", "max"), family="phone"),
    ModelDef("iphone_17_pro", "iPhone 17 Pro", _iph("17", "pro"), family="phone"),
    ModelDef("iphone_17", "iPhone 17", _iph("17"), family="phone"),
    # The Air took the Plus slot in the 17 generation and carries no number, so
    # it needs its own pattern rather than a _iph() call.
    ModelDef("iphone_air", "iPhone Air", r"\biphone\s+air\b", family="phone"),
)

_COMPILED = tuple((m, re.compile(m.pattern)) for m in REGISTRY)

DISPLAY = {m.key: m.display for m in REGISTRY}

# Generic keys used when a title omits VRAM, mapped to the specific SKUs they
# sit between. pricing.py borrows those pools when the generic key is short of
# comps of its own.
GENERIC_FALLBACKS = {
    "rtx_4060_ti": ("rtx_4060_ti_8g", "rtx_4060_ti_16g"),
    "rtx_5060_ti": ("rtx_5060_ti_8g", "rtx_5060_ti_16g"),
    "rx_9060_xt": ("rx_9060_xt_8g", "rx_9060_xt_16g"),
    # Single-variant VRAM splits. Listed here for the same reason as the pairs
    # above, but the load-bearing effect is in comps_loop: only models reachable
    # from a search (directly or through this map) are eligible to be judged
    # absent, so without an entry a 3060 12GB listing would never be closed and
    # could never contribute a sold comp.
    "rtx_3060": ("rtx_3060_12g",),
    "rtx_3080": ("rtx_3080_12g",),
}


def same_family(search_key: str, item_key: str | None) -> bool:
    """Does a classified listing satisfy a search that targeted `search_key`?

    Exact match, or the generic/specific pairing of a split-VRAM SKU — a search
    for `rx_9060_xt` must still accept a listing that classified as
    `rx_9060_xt_16g`.
    """
    if item_key is None:
        return False
    if item_key == search_key:
        return True
    if item_key in GENERIC_FALLBACKS.get(search_key, ()):
        return True
    return search_key in GENERIC_FALLBACKS.get(item_key, ())


@dataclass(frozen=True)
class Match:
    model_key: str | None
    display: str | None
    confidence: str  # 'high' | 'medium' | 'low' | 'none'
    vram: int | None = None
    family: str = "gpu"

    @property
    def priceable(self) -> bool:
        """Only high/medium matches may drive the margin engine."""
        return self.model_key is not None and self.confidence in ("high", "medium")


NO_MATCH = Match(None, None, "none")

# Confidence levels, weakest first. Used to *cap* a level rather than compare
# strings ad hoc, so "at most medium" is expressed once.
_CONFIDENCE_ORDER = ("none", "low", "medium", "high")


def _cap_confidence(level: str, ceiling: str) -> str:
    """`level`, but never stronger than `ceiling`."""
    if _CONFIDENCE_ORDER.index(level) <= _CONFIDENCE_ORDER.index(ceiling):
        return level
    return ceiling


def _vram_hits(norm_text: str) -> list[tuple[int, int, int]]:
    """Every plausible VRAM figure as (value, start, end) in `norm_text`."""
    return [(int(m.group(1)), m.start(), m.end()) for m in VRAM_RE.finditer(norm_text)]


def _gap(span: tuple[int, int], start: int, end: int) -> int:
    """Character distance between two non-overlapping spans, 0 if they touch."""
    if start >= span[1]:
        return start - span[1]
    if end <= span[0]:
        return span[0] - end
    return 0


def extract_vram(norm_text: str, near: tuple[int, int] | None = None) -> int | None:
    """The VRAM figure this text is asserting, or None.

    With `near` — the span the model number matched at — the figure *closest to
    the model number* wins. Titles routinely quote more than one memory size and
    only one of them belongs to the card: "RTX 4060 Ti 8GB - PC con 16GB RAM" is
    a real shape, and largest-wins read the system RAM as VRAM, classified it as
    rtx_4060_ti_16g, and then priced an 8GB card against the 16GB reference —
    manufacturing a bargain out of a correctly-priced card. Sellers write the
    card's memory next to the card's name; that adjacency is the signal.

    Without `near` there is no positional information to use (the caller is
    scanning a description as a fallback, where the model number is absent), so
    it degrades to the old largest-wins behaviour. That is the right default
    there: the alternative is picking arbitrarily, and for the split SKUs we only
    ever compare against 8/16.

    Ties in distance are broken toward the larger figure, purely so the function
    is deterministic; a genuine tie means the title is ambiguous either way.
    """
    hits = _vram_hits(norm_text)
    if not hits:
        return None
    if near is None:
        return max(value for value, _, _ in hits)
    return min(hits, key=lambda h: (_gap(near, h[1], h[2]), -h[0]))[0]


def _has_token(norm_text: str, tokens: tuple[str, ...]) -> bool:
    return any(re.search(rf"\b{re.escape(t)}\b", norm_text) for t in tokens)


def _confidence(norm_text: str, has_vram_proof: bool, key: str) -> str:
    """High when a brand token backs the number; lower for bare digits.

    The brand has to belong to the *right* vendor. A rival's branding next to a
    colliding model number is evidence against the match, not for it: "Nvidia
    7600GS" is a 2006 GeForce, not a Radeon RX 7600, and treating nvidia as
    generic proof made it a high-confidence AMD match worth 200 EUR.

    A wrong-vendor title drops to 'low', which `Match.priceable` refuses, so it
    can never reach the margin engine. It is not rejected outright — the number
    still matched, and a listing can legitimately name both vendors ("cambio mi
    RX 7600 por una Nvidia").
    """
    if key.startswith("iphone_"):
        # Phones have no rival-vendor collision to resolve: the pattern itself
        # requires the word "iphone", so the brand is already proven by the
        # match. There is no equivalent of a bare "4070" here.
        return "high" if _has_token(norm_text, APPLE_TOKENS) else "medium"

    if key in _CONSOLE_KEYS:
        # Same reasoning as phones, and stronger: the console patterns require
        # "ps5" / "xbox series x" outright, so there is no bare number to
        # mistake and no rival vendor whose branding could collide — Sony and
        # Microsoft do not make each other's consoles.
        #
        # Note this is not the whole test. classify() still refuses to price a
        # console whose title merely *mentions* it, which is what actually
        # separates a console from the game named after it.
        return "high"

    own, rival = (
        (AMD_TOKENS, NVIDIA_TOKENS) if key.startswith("rx_") else (NVIDIA_TOKENS, AMD_TOKENS)
    )
    if _has_token(norm_text, own):
        return "high"
    if _has_token(norm_text, rival):
        return "low"
    # Only generic tokens (grafica, gpu, vga...) — no vendor claim either way.
    if _has_token(norm_text, BRAND_TOKENS):
        return "high"
    if has_vram_proof:
        return "medium"
    return "low"


def _scan(
    norm_text: str,
    *,
    vram_fallback: str = "",
    brand_text: str | None = None,
    ceiling: str | None = None,
) -> Match | None:
    """Run the registry against one normalised string. None if nothing matched.

    Registry order does the disambiguation: every VRAM-gated entry sits above
    its generic fallback, which in turn sits above the shorter base-model
    pattern, so the first match is always the most specific one.

    `vram_fallback` is a second string to read VRAM from when `norm_text` quotes
    none; `brand_text` is what the confidence check reads (brand evidence is
    allowed to come from a different field than the model number); `ceiling`
    caps the confidence the scan may return.
    """
    brand_text = norm_text if brand_text is None else brand_text
    # One VRAM read per distinct match position, not per registry entry: a
    # "4060 ti" title is tried against the 16GB, 8GB and generic entries in turn
    # and they all match at the same offset.
    per_span: dict[tuple[int, int], int | None] = {}

    for model, rx in _COMPILED:
        hit = rx.search(norm_text)
        if hit is None:
            continue
        span = hit.span()
        if span not in per_span:
            vram = extract_vram(norm_text, near=span)
            if vram is None and vram_fallback:
                vram = extract_vram(vram_fallback)
            per_span[span] = vram
        vram = per_span[span]
        if model.vram is not None and vram != model.vram:
            continue
        confidence = _confidence(brand_text, model.vram is not None, model.key)
        if ceiling is not None:
            confidence = _cap_confidence(confidence, ceiling)
        return Match(model.key, model.display, confidence, vram, family=model.family)

    return None


def classify(title: str | None, description: str | None = None) -> Match:
    """Map a listing to a model — title first, description only as a last resort.

    A title match always wins and is never overridden: titles are what sellers
    put effort into and they name the thing being sold, while descriptions list
    the whole machine it came out of, the buyer's options, and what else the
    seller has for sale.

    The description pass exists because "Tarjeta gráfica Nvidia, pregunta por el
    modelo" style titles returned NO_MATCH, which made the listing unpriceable
    and therefore silent — and a vague title correlates with a seller who does
    not know what they have, which is exactly the population worth buying from.
    Every description-only match is capped at 'low', which Match.priceable
    refuses, so it can never reach the margin engine. It shipped at 'medium'
    (priceable) and the first live run produced three alerts, all three wrong,
    all three from this path:

        "Tarjeta Gráfica MSI GeForce GTX 1660"  -> priced as an RTX 3060
        "Samsung OLED S93F 48 4K TV"            -> priced as an RTX 5080
        "Nvlink P3651 NVIDIA"                   -> priced as an RTX 3090

    An earlier guard capped only descriptions naming *several* cards, on the
    theory that a single named card was reliable evidence. Those three each name
    exactly one, which is the refutation: a description mentioning one model is
    not evidence the listing is that model. It is a comparison ("rinde como una
    3060"), a compatibility note, a bridge that connects two of them, or another
    listing the seller is cross-selling. And the title — which does name the real
    product — was ignored precisely because it matched nothing in the registry.

    No downstream guard covers this. MIN_PLAUSIBLE_RATIO only catches gross
    mismatches; adjacent-tier confusion, the common case, sails through: a 300
    EUR listing described as a 4070 Ti Super (seed 520) sits well above
    0.35*520 = 182, its 240 EUR offer clears the ~392 ceiling, and it reads as a
    strong deal.

    Making this priceable again needs a title that names some *other*
    identifiable product to veto the description outright — a notion of "this
    title names a thing" the registry alone cannot express, since the registry
    knows only GPUs and these titles were TVs and cables. Until that exists the
    match is kept for observability and denied the margin engine. The winning
    key is still returned, so logs and same_family() see what was recognised; it
    is the confidence, and therefore the pricing, that is withheld. A title match
    is     """
    norm_title = normalise(title)
    if not norm_title:
        return NO_MATCH
    norm_desc = normalise(description)

    # VRAM is read from the title first; the description is a fallback because
    # it's far noisier (system RAM, other parts in a bundle, ...).
    match = _scan(norm_title, vram_fallback=norm_desc)
    if match is not None:
        # A console name in a title is not evidence the listing *is* a console.
        # It is overwhelmingly a game or an accessory naming its platform, and
        # junk.py catches only the ones that say so outright — "Mando PS5",
        # "FIFA 23 para Xbox Series X", anything naming two platforms. What is
        # left is the bare game title, "Elden Ring PS5", which carries no junk
        # signal at all and would otherwise price a 70 EUR disc as a console.
        #
        # So the same remedy the description-only branch below uses: keep the
        # match for observability, withhold priceability. Nothing unpriceable
        # can reach a comps pool, which is the only thing consoles are for.
        if match.family == "console" and not _console_titled(norm_title):
            return replace(match, confidence="low")
        return match

    if norm_desc:
        # Brand tokens are read from title + description together: the vague
        # titles this branch exists for usually do say "tarjeta gráfica" or
        # "Nvidia" and only omit the number, and that is real vendor evidence.
        # It also keeps the rival-vendor downgrade working across the two
        # fields, so an nvidia title with a "7600" in the description still
        # drops below priceable instead of pricing as a Radeon.
        # One card named in the description may price the listing; several may
        # not. See the docstring for why ambiguity is resolved by withholding
        # priceability rather than by picking a candidate.
        # Capped at 'low' — recognised, never priced. This branch shipped at
        # 'medium' (priceable) and the first live run produced three alerts,
        # all three of them wrong, all three from exactly this path:
        #
        #   "Tarjeta Gráfica MSI GeForce GTX 1660"  -> priced as an RTX 3060
        #   "Samsung OLED S93F 48 4K TV"            -> priced as an RTX 5080
        #   "Nvlink P3651 NVIDIA"                   -> priced as an RTX 3090
        #
        # The single-model ambiguity guard did not help, because each of those
        # descriptions names exactly one card. That is the real lesson: a
        # description mentioning one model is not evidence the listing *is* that
        # model. It is a comparison ("rinde como una 3060"), a compatibility
        # note, a bridge for two of them, or the seller's other listing — and
        # the title, which does name the actual product, was ignored precisely
        # because it failed to match anything in the registry.
        #
        # A title that names some *other* identifiable product should veto the
        # description outright, which needs a notion of "this title names a
        # thing" that the registry alone cannot express. Until that exists the
        # match is kept for observability and denied the margin engine.
        match = _scan(
            norm_desc,
            brand_text=f"{norm_title} {norm_desc}",
            ceiling="low",
        )
        if match is not None:
            return match

    return NO_MATCH


_CONSOLE_KEYS = frozenset(m.key for m in REGISTRY if m.family == "console")

FAMILY_BY_KEY = {m.key: m.family for m in REGISTRY}


def family_of(model_key: str | None) -> str | None:
    """Family for a stored model_key, for callers that kept only the key.

    The loops classify once and carry the Match around, so they read
    `match.family` directly. This exists for the paths that start from a
    persisted `model_key` — a comps search row, a model_prices row — and need to
    know whether they are looking at a card or a phone.
    """
    return FAMILY_BY_KEY.get(model_key or "")


# iPhone capacities, as sold. Written against the normalised text, where
# normalise() has already split letter/digit runs ("256GB" -> "256 gb").
STORAGE_RE = re.compile(r"\b(64|128|256|512)\s*gb\b|\b(1|2)\s*tb\b")


def extract_storage(text: str | None) -> str | None:
    """Capacity named in the text, as a canonical token ('256gb', '1tb').

    Recorded on the listing rather than folded into the model key. Capacity is
    the single biggest price driver within one iPhone model — a 15 Pro Max 1TB
    is hundreds of euros above the 256GB — so a pool mixing them is noisy, and
    splitting it later will need this column populated from the start.

    Not split into separate registry entries yet, the way GPU VRAM is: that
    quadruples the model count and thins every pool below MIN_COMPS before a
    single comp has been collected. Capture first, split once the data says the
    spread justifies it.
    """
    if not text:
        return None
    hit = STORAGE_RE.search(normalise(text))
    if hit is None:
        return None
    return f"{hit.group(1)}gb" if hit.group(1) else f"{hit.group(2)}tb"
