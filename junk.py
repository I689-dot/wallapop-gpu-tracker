"""Phrase-only junk filter (spec §8).

Deliberately never bans a single word: "roto", "piezas", "cambio" and friends
appear constantly in perfectly good listings ("no acepto cambios", "sin piezas
que falten"), so only multi-word phrases can exclude. `busco`/`compro`/`se
busca` are wanted-ads and only count at the *start* of a title, which keeps a
seller writing "...no busco cambios" safe.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from models import CONSOLE_LEAD_NOUNS, normalise

# Phrases are matched against the accent-stripped, punctuation-collapsed text,
# so they're written here in the same normalised form (no accents).
DEFECT = (
    "no funciona",
    "para piezas",
    "no enciende",
    "no arranca",
    "no da imagen",
    "no da video",  # "enciende pero no da video" is the "no da imagen" variant
                    # that was missing — live testing caught real dead cards
                    # (powers on, no video output) passing the filter clean and
                    # topping the bot's own highest-margin alerts.
    "sin funcionar",
    "para reparar",
    "no probada",
    "no probado",
    "non testee",  # foreign-language relistings turn up in Spanish results
    "solo caja",
    "caja vacia",
    "solo la caja",
    "no se vende",
    # Faults that read as ordinary prose and so never tripped the phrases
    # above. Every one is taken from a confirmed sale that entered a comps
    # pool at full weight: "PlayStation 5 (PS5) para arreglar" (150 EUR
    # against a 349 EUR median), "PlayStation 5 Disco con fallo DNS" (170),
    # "iPhone 15 ... se reinicia cada 3 minutos" (160 against 400), "iPhone 15
    # Pro Max ... el telefono no se puede usar" (150 against 450).
    #
    # A broken unit and a working one are not the same market, so this is not
    # a bargain to be alerted on — it is a different product wearing the same
    # name. These sit in DEFECT rather than in any family branch because
    # _shared_rules runs for phones, consoles and cards alike, and a fault is
    # a fault whatever broke.
    "para arreglar",
    "se reinicia",
    "no se puede usar",
    "con fallo",
    # Sold as spare parts. "piezas" alone can never exclude — "sin piezas que
    # falten" is a working card describing itself — so this matches only the
    # phrase that opens a parts listing: "Piezas para iPhone 15 Plus".
    "piezas para",
    # iCloud-locked handsets. The phone is intact and worthless.
    "bloqueado por icloud",
    "bloqueo de icloud",
    "cuenta de icloud",
)

TRADE = (
    "cambio por",
    "solo cambio",
)

NOT_A_CARD = (
    "solo disipador",
    "solo ventilador",
    "waterblock",
    "backplate",
    "solo soporte",
    "soporte vertical",
    "soporte grafica",      # the 11 EUR mounting bracket, not the card
    "soporte para grafica",
    "soporte gpu",
    "riser",
)

# Whole-PC / bundle listings. These name a GPU in the title and classify to it
# perfectly, but "RTX 4070 Super" at 850 EUR inside a full tower is not a comp
# for a loose 4070 Super — left in, they drag every median upwards and blow up
# the ceilings. Title-only, because a card's *description* often mentions the PC
# it came out of.
BUNDLE = (
    "pc gaming",
    "pc gamer",
    "ordenador gamer",
    "torre gamer",
    "equipo gamer",
    "pc completo",
    "pc sobremesa",
    "pc montado",
    "pc torre",
    "ordenador gaming",
    "ordenador sobremesa",
    "ordenador completo",
    "equipo completo",
    "equipo gaming",
    "torre gaming",
    "sobremesa gaming",
    "setup completo",
    # "Ordenador De Sobremesa Corsair AMD Ryzen 5 7600" — the article between
    # the two words defeated "ordenador sobremesa".
    "ordenador de sobremesa",
)

# A second component of comparable value, sold with the card. Title-only, and —
# unlike COMPONENT_TOKENS below — *not* skipped when the title opens with a card
# noun, because these listings do open with the card: "Tarjeta Nvidia 4060 +
# Ryzen 7 + Líquida" (500 EUR), "Fuente Alimentación y Gráfica MSI 4060" (200),
# "RTX 3060 Ti Zotac + Fuente Seasonic 650W Gold" (200), "NVIDIA RTX 3060 12GB +
# Fuente 750W" (190). All four are confirmed sales, all four classified to the
# card at high confidence, and in every one the price bought two things.
#
# Kept to parts that cost real money on their own and can never be an attribute
# of a graphics card. "disipador", "ssd" and "caja" are deliberately absent: a
# card legitimately ships "con disipador nuevo" or "con su caja", and those stay
# in COMPONENT_TOKENS where a card-noun lead still rescues them.
COMBO_TOKENS = ("fuente", "fuentes")

# A CPU named alongside the card. The tier digit must close on a word boundary,
# which is what separates "+ Ryzen 7 + Líquida" (a bundle) from "ideal para
# Ryzen 5000" (a compatibility note on a real card) — in the latter the digit
# run continues and the pattern does not fire.
# The optional leading group is what separates a bundle from a compatibility
# note. "Tarjeta Nvidia 4060 + Ryzen 7 + Líquida" names the CPU as a second
# product (normalise() eats the "+", so nothing precedes it); "RX 6700 XT
# compatible con Ryzen 7 5800X" names it as advice to the buyer. check() reads
# group(1) and only treats the match as a bundle when it is absent.
#
# The tier digit must also close on a word boundary, which is what keeps "ideal
# para Ryzen 5000" out: there the digit run continues and nothing matches.
COMBO_CPU_RX = re.compile(
    r"\b(?:(compatible\s+con|ideal\s+para|apto\s+para|admite|soporta|para|con)"
    r"\s+)?(?:ryzen|intel\s+core|core\s+i)\s*[3579]\b"
)

# Mass storage quoted in the title of a graphics card. A card has VRAM, measured
# in GB and topping out at 32; it has never had a terabyte of anything, so a
# TB figure means the title is describing a machine the card sits inside.
#
# This is what catches the system-integrator listings that name no CPU brand and
# no "PC" noun at all: "PepiPC NIEVE AZUL 7800X3D 32GB 1TB RX 9070 XT 16GB"
# (1799 EUR, filed as an RX 9070 XT comp against a 640 EUR median). Its Ryzen is
# written "7800X3D" with no "ryzen" in front, which is why the CPU patterns miss
# it; the "1TB" is unambiguous.
#
# normalise() splits the digit/letter boundary, so "1TB" arrives as "1 tb".
STORAGE_TB_RX = re.compile(r"\b\d{1,2}\s*tb\b")

# If one of these leads the title, it's a card being sold that merely mentions
# the kind of PC it suits ("Grafica RTX 4070 para PC gaming") — not a bundle.
# "placa grafica" is the Latin-American name for the same product and turns up
# verbatim on Spanish listings ("Placa Gráfica Gigabyte RTX 5070 ICE", a real
# 589 EUR sale). It must lead this tuple, and it must be here at all: without
# it the title opens with "placa", fails _leads_with_card_noun, and is then
# rejected by COMPONENT_TOKENS' "placa" — a graphics card thrown out for being
# called a graphics card. "placa base" is unaffected: it does not start with
# "placa grafica", so the motherboard rule still fires.
CARD_NOUNS = (
    "placa grafica", "tarjeta grafica", "tarjeta", "grafica", "graficas",
    "gpu", "vga",
)

# Gaming laptops are the worst comps polluter of all: they name a desktop GPU in
# the title and sell for 850-2600 EUR, which would roughly double a 4070's
# median. Bare tokens are used here — against the phrase-only rule that governs
# the defect list — but only for words that are laptop *product lines* and can
# never appear on a graphics card. Deliberately excluded from this list are the
# AIB brand words that would cause false hits: nitro (Sapphire Nitro+), rog,
# tuf, strix, aorus, pulse, ventus, eagle, phantom, hellhound.
LAPTOP_TOKENS = (
    "portatil", "portatiles", "notebook", "laptop",
    "legion", "zephyrus", "thinkpad", "ideapad", "vivobook", "zenbook",
    "macbook", "victus", "alienware", "helios", "katana",
    "cyborg", "elitebook", "probook", "latitude", "inspiron", "pavilion",
)

LAPTOP_PHRASES = (
    "ordenador portatil",
    "gaming laptop",
    "portatil gaming",
    "omen by hp",
    "predator helios",
    "rog strix g",
    "proart studiobook",
)

# CPU listings, not GPUs — AMD Ryzen model numbers can numerically collide
# with Radeon GPU model numbers with no differentiating suffix (Ryzen 5 "7600"
# vs Radeon RX "7600"), which let a processor get misclassified as a graphics
# card. Deliberately narrow (not "ryzen" or "amd"): those brand names can
# legitimately appear in a GPU title mentioning compatibility ("ideal para
# Ryzen 5000"), and being too strict here costs real GPU deals. "procesador"/
# "microprocesador" only ever show up when the product itself is a CPU.
CPU_TOKENS = ("procesador", "microprocesador")

# Desktop CPU model numbers, which collide with Radeon numbers head-on: a
# "Ryzen 5 7600X" is a processor and an "RX 7600 XT" is a graphics card, and the
# digits are identical.
#
# CPU_TOKENS above was documented as the defence for this and is not: it only
# fires on the literal words "procesador"/"microprocesador", and the laptop
# regex below only catches *mobile* chips (the h/hs/hx suffixes). A plain
# "AMD Ryzen 5 7600X" hit neither, matched \b7600\b, and — because "amd" is a
# valid AMD brand token — classified as an RX 7600 at *high* confidence,
# priceable, against a ~200 EUR reference. Verified live on 2026-08-26.
#
# Kept narrow in two ways, because the original comment's caution still stands
# ("ideal para Ryzen 5000" is a real GPU listing):
#   * the number must have a CPU's shape — 4-5 digits (Ryzen 7600, Intel
#     12400) with an optional x/x3d/g/k/f
#     suffix — so a bare series like "ryzen 5000" does not match;
#   * it is skipped entirely when the title also names a GPU vendor, so
#     "RX 6700 compatible con Ryzen 7 5800X" survives.
#
# The tier digit is optional after "ryzen" because sellers drop it: "Pack 32GB
# DDR5 + Ryzen 7600x + B650" is a CPU/RAM/motherboard bundle with no graphics
# card in it at all, and it sold for 395 EUR as an RX 7600 comp. "ryzen 7600"
# has no tier, so the old pattern — which required one — never saw it.
DESKTOP_CPU_RX = re.compile(
    r"\b(?:ryzen\s*[3579]?|i\s*[3579])\s*\d{4,5}\s*(?:x3d|xt|x|g|ge|k|kf|kd|f)?\b"
)

# GPU vendor words that make a CPU model number incidental rather than the
# product. Deliberately excludes bare "amd", which is exactly what a Ryzen box
# says.
GPU_VENDOR_TOKENS = ("rtx", "gtx", "geforce", "radeon", "rx", "nvidia")

# Other PC components sold on the same searches. None of these are graphics
# cards, and none were being filtered — "Placa base B550", "Disco duro SSD 1TB"
# and "Pack cables PSU" all reached the bootstrap alert path clean, where a
# keyword match and a price under the cap is the whole test.
#
# Title-only, and skipped when the title opens by naming a card, for the same
# reason the laptop rules are: a graphics card listing may legitimately mention
# these in passing ("RTX 3080 con disipador nuevo", "incluyo disco duro").
# Matching them against the description would reject real cards for describing
# what comes in the box.
COMPONENT_TOKENS = (
    "placa",          # "placa base"
    "disipador",
    "ssd",
    "nvme",
    "disco",
)

COMPONENT_PHRASES = (
    "placa base",
    "disco duro",
    "pack cables",
    "bloque refrigeracion",
    "refrigeracion liquida",
    # A 2006 GeForce, not a Radeon RX 7600. The rival-vendor rule in models.py
    # already drops "Nvidia 7600GS" to low confidence; this stops it earlier and
    # covers the variant that names no vendor at all. normalise() splits the
    # letter/digit boundary, so the stored phrase is "7600 gs".
    "7600 gs",
    # Old Radeon, out of the tracked registry entirely.
    "vega rx",
    "rx vega",
)

# Phone accessories and services, not phones. These are the dominant noise on
# any iPhone search: a "Funda iPhone 15" at 8 EUR against a 700 EUR reference is
# a 99% margin by the maths and pure junk in reality — it is the listing named
# in db.log_junk's own docstring as what filled that table with 2.86M rows.
#
# Split in two, because this file's founding rule is that a single word may
# never exclude. "iPhone 15 con funda incluida" is a real phone sold with a
# case, and banning the bare word "funda" kills it — a working listing rejected
# for describing an extra, and invisible, because exclusions are never alerted.
#
# So an accessory noun only excludes when the title *opens* with it, which is
# how these listings are actually written ("Funda iPhone 15 Pro"). "Replica" and
# "clon" sit here rather than in the phrase list for the same reason: sellers
# write "100% original, no replica" constantly.
PHONE_ACCESSORY_PREFIXES = frozenset({
    "funda", "fundas", "carcasa", "protector", "protectores",
    "cargador", "cargadores", "cable", "cables", "adaptador",
    "pantalla", "pantallas", "bateria", "baterias", "tapa",
    "camara", "placa", "soporte", "cristal", "replica", "clon",
    "maqueta", "imitacion", "repuesto", "repuestos",
})

# ---------------------------------------------------------------- consoles
# PS5 and Xbox Series X are tracked for comps only (family 'console',
# alert_loop.ALERTING_FAMILIES), and the noise on a console search is unlike
# anything else this bot searches. Sampling `ps5` and `xbox series x` live on
# 2026-08-26, roughly four listings in five carrying the console's name were a
# *game* or an accessory: "Elden Ring PS5", "Mando DualSense PS5", "FIFA 23
# para Xbox Series X", and — genuinely — "Nevera Consola XBOX Series X 10L", a
# novelty fridge.
#
# That matters more here than a bad GPU alert would. A game is a real listing
# at a real price that really sells, so it does not look wrong to any of the
# sale-inference machinery; it just quietly drags a console's reference price
# toward 70 EUR. The pool has no way to notice.
#
# Note normalise() has already destroyed the slash that makes cross-platform
# listings obvious to a human: "PS4/PS5" arrives as "ps 4 ps 5" and "Series
# X/S" as "series x s". The rules below work on that shape, not on the
# punctuation.

# Nouns that open an accessory listing. Prefix-only, following the same
# founding rule as PHONE_ACCESSORY_PREFIXES: "Xbox Series X 1TB SSD con 2
# mandos" is a console sold with controllers and must survive, while "Mando
# Xbox Series X" must not.
CONSOLE_ACCESSORY_PREFIXES = frozenset({
    "mando", "mandos", "control", "controlador", "controladores", "dualsense",
    "dualshock", "auricular", "auriculares", "cascos", "volante", "volantes",
    "funda", "fundas", "carcasa", "carcasas", "soporte", "soportes", "base",
    "cargador", "cargadores", "cable", "cables", "adaptador", "grip", "grips",
    "protector", "protectores", "bateria", "ventilador", "refrigerador",
    "dock", "estacion", "kit", "steelbook", "poster", "posters", "pegatina",
    "pegatinas", "camiseta", "taza", "llavero", "lampara", "mochila", "logo",
    "nevera", "figura", "funko", "maqueta", "replica", "skin", "vinilo",
    "juego", "juegos", "disco", "caja", "cristal", "tapa", "recambio",
})

# Words that only ever belong to a game or an accessory. Skipped when the title
# leads with the console itself, because "Consola Xbox Series X + 2 Mandos y
# Juegos" is a console being sold with its games.
CONSOLE_NOT_A_CONSOLE_TOKENS = (
    "juego", "juegos", "mando", "mandos", "dualsense", "dualshock",
    "auriculares", "cascos", "volante", "steelbook", "funda", "carcasa",
    "grips", "nevera", "funko",
)

# Nouns that open a *description* belonging to a game or an accessory. This is
# the one rule allowed to override a console-led title, and it exists because
# the title alone genuinely cannot decide: "Play5 oni Rafa" opens with "play 5",
# passes _leads_with_console_noun, names no game, carries no junk signal, and
# classified as a PS5 at high confidence — a 60 EUR game inside a pool whose
# median is 349. Its description opens "Videojuego para PlayStation 5".
#
# Overriding is safe in this direction only. A real console's description opens
# by naming the console ("Consola de videojuegos PlayStation 5 Pro en color
# blanco"), never by naming a game — note that "consola de videojuegos" starts
# with "consola", so the console listing is untouched while "videojuego para..."
# is caught. Prefix-only, exactly like CONSOLE_ACCESSORY_PREFIXES.
CONSOLE_DESC_LEAD_PREFIXES = (
    "videojuego", "videojuegos", "juego para", "juegos para", "juego de",
    "pack de juegos", "pack de videojuegos", "lote de juegos",
    "memoria", "disco duro", "disco ssd", "tarjeta de memoria",
    "mando", "mandos", "auriculares", "cascos", "volante", "funda", "carcasa",
    "soporte", "cargador", "base de carga", "steelbook", "adaptador",
)

# Phrases that only ever appear in a description written about a game. Unlike
# the prefixes above these may sit anywhere in the text, so they are applied
# only when the title does *not* lead with the console — the same guard the
# title-side CONSOLE_NOT_A_CONSOLE_TOKENS check already uses, and for the same
# reason: a real console's description says "mando" and "juegos" constantly
# ("se entrega con 2 mandos y 3 juegos"), so no ordinary game word can be
# decisive here. These four are not ordinary: a console is never sold as "el
# juego base", and nothing but a collector's edition ships a "steelbook".
CONSOLE_DESC_GAME_PHRASES = (
    "del juego", "juego base", "steelbook", "edicion premium del",
)

# The card equivalent, and the same shape: a description that opens by naming
# the packaging is selling the packaging. "Rtx 4070 ti CAJA" sold for 5 EUR
# against a 450 EUR median with the description "Caja rtx 4070 ti" — the
# existing "solo caja" / "caja vacia" phrases both missed it because the seller
# wrote neither.
CARD_DESC_LEAD_PREFIXES = ("caja", "cajas", "embalaje", "funda", "soporte")

# "para PS5" means the listing is *for* the console, not the console. A console
# listing never says it.
CONSOLE_FOR_RX = re.compile(
    r"\bpara\s+(?:la\s+|el\s+|tu\s+)?"
    r"(?:ps\s*[45]|play\s*station\s*[45]|playstation\s*[45]|xbox|consola)\b"
)

# One listing, two platform generations, means a game — a console is exactly
# one platform. This is the single most reliable rule here, and it survives
# normalise() flattening the slash.
PLATFORM_RXS = {
    "ps5":       re.compile(r"\bps\s*5\b|\bplay\s*(?:station\s*)?5\b"),
    "ps4":       re.compile(r"\bps\s*4\b|\bplay\s*(?:station\s*)?4\b"),
    "ps3":       re.compile(r"\bps\s*3\b|\bplay\s*(?:station\s*)?3\b"),
    "ps2":       re.compile(r"\bps\s*2\b|\bplay\s*(?:station\s*)?2\b"),
    "psp":       re.compile(r"\bpsp\b|\bps\s*vita\b"),
    "xbox_sx":   re.compile(r"\bseri[ea]s?\s+x\b"),
    "xbox_ss":   re.compile(r"\bseri[ea]s?\s+s\b|\bseri[ea]s?\s+x\s+s\b"),
    "xbox_one":  re.compile(r"\bxbox\s+one\b"),
    "xbox_360":  re.compile(r"\bxbox\s+360\b"),
    "nintendo":  re.compile(r"\bnintendo\b|\bswitch\b|\bwii\b"),
    "pc":        re.compile(r"\bsteam\s+deck\b"),
}


# Phrases that mean "not a sellable handset" wherever they appear. Multi-word,
# so each carries its own context and cannot straddle an innocent sentence.
PHONE_NOT_A_PHONE = (
    "protector de pantalla",
    "cristal templado",
    "cambio de pantalla",
    "reparacion de",
    "reparamos",
    "pantalla para",
    "bateria para",
    "cargador para",
    "tapa trasera",
    "camara para",
    "solo la placa",
    "placa base",
    # An iCloud-locked handset cannot legally be resold and goes for a fraction
    # of a working one. Letting one price the pool drags the reference down for
    # every clean handset of that model.
    "libre de icloud",
    "bloqueado por icloud",
    "bloqueado icloud",
    "cuenta icloud",
)


# Patterns are matched against the *normalised* title, where normalise() has
# already split letter/digit runs ("i7 14650HX" -> "i 7 14650 hx").
LAPTOP_REGEXES = (
    # Laptop line followed by a screen size — "Aorus 17", "Sword 17", "Omen 16".
    # The lookahead stops "Nitro+ 16 GB" (a real graphics card) from matching.
    r"\b(?:aorus|omen|sword|raider|stealth|vector|titan|swift|nitro|predator|"
    r"crosshair|delta|modern|prestige|summit)\s*1[3-8]\b(?!\s*(?:gb|g)\b)",
    # Asus TUF A15/F15 laptops. TUF *cards* are "TUF Gaming OC", never "TUF A15",
    # so this stays clear of the GPU brand.
    r"\btuf\s*[af]\s*1[3-8]\b",
    # Asus ProArt *laptops* are the P-series ("ProArt P16", "ProArt PX13").
    # "proart" alone is left out of LAPTOP_TOKENS because ProArt is also a real
    # desktop GPU sub-brand ("ASUS ProArt GeForce RTX 4080 OC") — only the
    # laptop model-number pattern is banned, not the bare word.
    r"\bproart\s+px?\s*1[3-8]\b",
    # Mobile CPU suffixes — i7 14650HX, Ryzen 7 7840HS.
    r"\b(?:i\s*[3579]|ryzen\s*[3579])\s*\d{4,5}\s*(?:hx|hs|h)\b",
    # Explicit screen size.
    r"\b1[3-8]\s*(?:pulgadas|inch)\b",
)

_LAPTOP_RX = tuple(re.compile(p) for p in LAPTOP_REGEXES)

# A shouted "LEER" ("read [this]") in the *title* is Spanish-marketplace
# shorthand for "there's a catch, read the description" and in practice flags
# a real defect — "LEER Gigabyte RTX 3080 Ti Tarjeta Grafica" and "(LEERRR)
# URGE VENTA Gigabyte AORUS RTX 3080 Ti" are both real listings that hid a
# fault below the fold. Title only, deliberately: the *description* legitimately
# says "leer la descripcion" / "puedes leer mas abajo" constantly, and banning
# it there would gut half the listing pool. normalise() already lowercases and
# strips punctuation, so "(LEERRR)" arrives here as "leerrr". The shape is
# l + one-or-more e + one-or-more r, which catches leer/leeer/leerr/leerrr,
# but the leading/trailing \b keeps it off real words that merely contain the
# substring, e.g. "releer" (starts with r, not l) or "leerlo" (trailing "lo"
# breaks the boundary).
LEER_RX = re.compile(r"\ble+r+\b")

# Only rejected when the title *starts* with one of these.
WANTED_PREFIXES = (
    "busco",
    "compro",
    "se busca",
    "se compra",
    "necesito",
)

PHRASE_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("DEFECT", DEFECT),
    ("TRADE", TRADE),
    ("NOT_A_CARD", NOT_A_CARD),
)


# Phrases are matched on word boundaries, never as bare substrings. A plain
# `phrase in haystack` straddles words and silently kills good listings:
# normalise() strips the tilde from "año", so the extremely ordinary Spanish
# sentence "comprada hace 1 año, funciona perfecta" becomes
# "... 1 ano funciona perfecta", in which "a[no funciona]" matches DEFECT's
# "no funciona" as a substring. That is the worst possible failure — a working
# card, described as working, excluded for saying so, and invisible because
# exclusions are never alerted on.
def _compile(phrases: tuple[str, ...]) -> tuple[tuple[str, re.Pattern[str]], ...]:
    return tuple((p, re.compile(rf"\b{re.escape(p)}\b")) for p in phrases)


_PHRASE_GROUPS_RX = tuple(
    (category, _compile(phrases)) for category, phrases in PHRASE_GROUPS
)
_BUNDLE_RX = _compile(BUNDLE)
_PHONE_NOT_A_PHONE_RX = _compile(PHONE_NOT_A_PHONE)
_LAPTOP_PHRASES_RX = _compile(LAPTOP_PHRASES)
_COMPONENT_PHRASES_RX = _compile(COMPONENT_PHRASES)
_CONSOLE_DESC_GAME_RX = _compile(CONSOLE_DESC_GAME_PHRASES)

# Any mention of a tracked console, used only to route into the console branch.
_CONSOLE_TOKEN_RX = re.compile(
    r"\bps\s*5\b|\bplay\s*(?:station\s*)?5\b"
    r"|\bxbox\b(?=.*\bseri[ea]s?\s+[xs]\b)|\bseri[ea]s?\s+x\b(?=.*\bxbox\b)"
)


def _first_hit(
    haystack: str, compiled: tuple[tuple[str, re.Pattern[str]], ...]
) -> str | None:
    for phrase, rx in compiled:
        if rx.search(haystack):
            return phrase
    return None


@dataclass(frozen=True)
class JunkVerdict:
    excluded: bool
    phrase: str | None = None
    category: str | None = None


CLEAN = JunkVerdict(False)


def _is_phone_title(norm_title: str) -> bool:
    """Does this title name an iPhone?

    Deliberately just the word. Every phone rule that follows is an accessory or
    service pattern, so a false positive here costs nothing — a graphics card
    whose title says "iphone" is not a graphics card either — while a false
    negative lets "Funda iPhone 15" reach the margin engine at a 99% margin.
    """
    return re.search(r"\biphone\b", norm_title) is not None


def _leads_with_card_noun(norm_title: str) -> bool:
    """True when the title opens by naming a graphics card."""
    head = " ".join(norm_title.split()[:3])
    return any(head.startswith(noun) for noun in CARD_NOUNS)


def _is_console_title(norm_title: str) -> bool:
    """Does this title name a PS5 or an Xbox Series console?

    Like _is_phone_title, a false positive is cheap: every console rule that
    follows is an accessory, game or cross-platform pattern, and a graphics card
    whose title says "ps5" is not a graphics card either.
    """
    return _CONSOLE_TOKEN_RX.search(norm_title) is not None


def _opens_with(norm_text: str, prefixes: tuple[str, ...]) -> str | None:
    """The prefix `norm_text` opens with, or None.

    Whole-word: the text must either *be* the prefix or continue with a space,
    so "cajamarca" never counts as "caja".
    """
    for prefix in prefixes:
        if norm_text == prefix or norm_text.startswith(prefix + " "):
            return prefix
    return None


def _leads_with_console_noun(norm_title: str) -> bool:
    """True when the title opens by naming the console itself.

    This is the single most useful signal on a console search, and it comes
    straight from how the two kinds of listing are actually written. A console
    leads with what it is — "Consola PS5 con mando", "Xbox Series X 1TB SSD con
    2 mandos", "Ps5 pro 2tb blanca sin caja". A game leads with the game —
    "Elden Ring PS5", "Hogwarts Legacy Xbox Series X" — and mentions the
    platform afterwards, because that is the word buyers search for.
    """
    head = " ".join(norm_title.split()[:3])
    return any(head.startswith(noun) for noun in CONSOLE_LEAD_NOUNS)


def _platforms_named(norm_title: str) -> int:
    """How many distinct platform generations this title names."""
    return sum(1 for rx in PLATFORM_RXS.values() if rx.search(norm_title))


def check(title: str | None, description: str | None = None) -> JunkVerdict:
    """Return why a listing should be dropped, or CLEAN."""
    norm_title = normalise(title)
    norm_desc = normalise(description) if description else ""
    haystack = f"{norm_title} {norm_desc}".strip()

    # Phone rules first, and only for titles naming an iPhone. A phone listing
    # has nothing to do with the bundle/laptop/CPU rules below, and those rules
    # have nothing to say about it.
    if _is_phone_title(norm_title):
        words = norm_title.split()
        if words and words[0] in PHONE_ACCESSORY_PREFIXES:
            return JunkVerdict(True, words[0], "NOT_A_PHONE")
        hit = _first_hit(norm_title, _PHONE_NOT_A_PHONE_RX)
        if hit:
            return JunkVerdict(True, hit, "NOT_A_PHONE")
        return _shared_rules(norm_title, haystack)

    # Console rules, and only for titles naming a tracked console. Same shape as
    # the phone branch: these listings have nothing to do with the bundle,
    # laptop and CPU rules below, and those rules have nothing to say about
    # them.
    if _is_console_title(norm_title):
        words = norm_title.split()
        if words and words[0] in CONSOLE_ACCESSORY_PREFIXES:
            return JunkVerdict(True, words[0], "NOT_A_CONSOLE")

        # A description that opens by naming a game or an accessory decides the
        # listing outright, ahead of every title rule below — including the
        # console-led exemption. See CONSOLE_DESC_LEAD_PREFIXES for why this one
        # is allowed to override a title that looks like a console.
        hit = _opens_with(norm_desc, CONSOLE_DESC_LEAD_PREFIXES)
        if hit:
            return JunkVerdict(True, hit, "NOT_A_CONSOLE")

        # Two platforms named at once is a game, whatever else the title says —
        # this one is not skipped for console-led titles, because "Xbox Series
        # X/S" leads with the console and is still a game.
        if _platforms_named(norm_title) > 1:
            return JunkVerdict(True, "multi-platform", "NOT_A_CONSOLE")

        if CONSOLE_FOR_RX.search(norm_title):
            return JunkVerdict(True, "para", "NOT_A_CONSOLE")

        # The remaining words are only decisive when the title does not lead
        # with the console: a console legitimately ships "con 2 mandos y juegos".
        if not _leads_with_console_noun(norm_title):
            tokens = set(words)
            for token in CONSOLE_NOT_A_CONSOLE_TOKENS:
                if token in tokens:
                    return JunkVerdict(True, token, "NOT_A_CONSOLE")
            # Same guard, now reading the description: a title that declines to
            # say "console" plus a description written about a game is a game.
            hit = _first_hit(norm_desc, _CONSOLE_DESC_GAME_RX)
            if hit:
                return JunkVerdict(True, hit, "NOT_A_CONSOLE")

        return _shared_rules(norm_title, haystack)

    # Form-factor checks run on the title only and are skipped when the title
    # opens by naming a card, so "Gráfica RTX 4070 sacada de un portátil" stays.
    if not _leads_with_card_noun(norm_title):
        hit = _first_hit(norm_title, _BUNDLE_RX)
        if hit:
            return JunkVerdict(True, hit, "BUNDLE")

        tokens = set(norm_title.split())
        for token in CPU_TOKENS:
            if token in tokens:
                return JunkVerdict(True, token, "CPU")
        if not any(v in tokens for v in GPU_VENDOR_TOKENS):
            cpu_hit = DESKTOP_CPU_RX.search(norm_title)
            if cpu_hit:
                return JunkVerdict(True, cpu_hit.group(0), "CPU")
        for token in LAPTOP_TOKENS:
            if token in tokens:
                return JunkVerdict(True, token, "LAPTOP")
        hit = _first_hit(norm_title, _LAPTOP_PHRASES_RX)
        if hit:
            return JunkVerdict(True, hit, "LAPTOP")
        for rx in _LAPTOP_RX:
            hit = rx.search(norm_title)
            if hit:
                return JunkVerdict(True, hit.group(0), "LAPTOP")

        for token in COMPONENT_TOKENS:
            if token in tokens:
                return JunkVerdict(True, token, "COMPONENT")
        hit = _first_hit(norm_title, _COMPONENT_PHRASES_RX)
        if hit:
            return JunkVerdict(True, hit, "COMPONENT")

    # A card sold together with a second component of comparable value. Outside
    # the card-noun guard above, deliberately: these listings *do* open by
    # naming the card ("Tarjeta Nvidia 4060 + Ryzen 7 + Líquida"), which is
    # exactly why the guard let them through. Placed after that block rather
    # than before it so a laptop stays attributed to LAPTOP and a processor to
    # CPU — the category is what db.log_junk records, and a listing filed under
    # the wrong reason is a listing nobody can audit later. See COMBO_TOKENS.
    title_tokens = set(norm_title.split())
    for token in COMBO_TOKENS:
        if token in title_tokens:
            return JunkVerdict(True, token, "BUNDLE")
    combo_cpu = COMBO_CPU_RX.search(norm_title)
    if combo_cpu and not combo_cpu.group(1):
        return JunkVerdict(True, combo_cpu.group(0), "BUNDLE")
    storage = STORAGE_TB_RX.search(norm_title)
    if storage:
        return JunkVerdict(True, storage.group(0), "BUNDLE")

    # A description that opens by naming the packaging is selling the packaging.
    hit = _opens_with(norm_desc, CARD_DESC_LEAD_PREFIXES)
    if hit:
        return JunkVerdict(True, hit, "NOT_A_CARD")

    return _shared_rules(norm_title, haystack)


def _shared_rules(norm_title: str, haystack: str) -> JunkVerdict:
    """Rules that hold whatever the product is: wanted ads, LEER, defects.

    Split out from check() so the wanted-ad, LEER and defect rules stay in one
    place regardless of which product-specific branch ran first.
    """
    for prefix in WANTED_PREFIXES:
        if norm_title.startswith(prefix + " ") or norm_title == prefix:
            return JunkVerdict(True, prefix, "WANTED")

    leer_hit = LEER_RX.search(norm_title)
    if leer_hit:
        return JunkVerdict(True, leer_hit.group(0), "LEER")

    for category, compiled in _PHRASE_GROUPS_RX:
        hit = _first_hit(haystack, compiled)
        if hit:
            return JunkVerdict(True, hit, category)

    return CLEAN
