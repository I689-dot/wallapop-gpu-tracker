"""Two additions to the junk filter: the "no da video" defect phrase and the
title-only "LEER" rule. See junk.py for the reasoning; these tests just pin
the behaviour, especially the regression guard against the description's
legitimate "leer la descripcion".
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import junk  # noqa: E402
import models  # noqa: E402


# ----------------------------------------------------------------- no da video
@pytest.mark.parametrize(
    "title,description",
    [
        ("RTX 3080 Ti enciende pero no da video", None),
        ("Gigabyte RTX 3070 Ti", "Enciende pero no da video, para piezas o reparar"),
    ],
)
def test_no_da_video_is_excluded(title, description):
    verdict = junk.check(title, description)
    assert verdict.excluded and verdict.category == "DEFECT"


# ----------------------------------------------------------------------- LEER
@pytest.mark.parametrize(
    "title",
    [
        "LEER Gigabyte RTX 3080 Ti Tarjeta Grafica",
        "(LEERRR) URGE VENTA Gigabyte AORUS RTX 3080 Ti",
    ],
)
def test_leer_titles_are_excluded(title):
    """Real listings — a shouted LEER in the title means there's a catch."""
    verdict = junk.check(title)
    assert verdict.excluded and verdict.category == "LEER"


def test_leer_in_description_does_not_exclude():
    """Regression guard: 'leer la descripcion' is boilerplate that appears in
    countless good listings and must never be checked against the LEER rule."""
    verdict = junk.check(
        "Tarjeta Grafica MSI RTX 4070",
        "Antes de preguntar, puedes leer la descripcion completa mas abajo.",
    )
    assert not verdict.excluded, f"wrongly excluded on {verdict.phrase!r} ({verdict.category})"


# ------------------------------------------------------------------- controls
@pytest.mark.parametrize(
    "title",
    [
        "Tarjeta Grafica MSI RTX 4070",
        "RTX 4070 Dual ASUS 12GB",
    ],
)
def test_ordinary_good_listings_stay_clean(title):
    verdict = junk.check(title)
    assert not verdict.excluded, f"wrongly excluded on {verdict.phrase!r} ({verdict.category})"


def test_cosmetic_oxidation_is_not_junk():
    """Owner's explicit principle: as long as the card works, it's fine. A
    cosmetically corroded HDMI port on an otherwise working card is a
    legitimate discount, not a defect — 'oxidada' must never be banned."""
    verdict = junk.check(
        "RTX 3070 en buen estado",
        "Un poco oxidada la placa donde se conecta el HDMI pero funciona bien.",
    )
    assert not verdict.excluded, f"wrongly excluded on {verdict.phrase!r} ({verdict.category})"


def test_sin_garantia_is_not_junk():
    """'sin garantia' usually just means no shop warranty, not a defect."""
    verdict = junk.check("RTX 4060 Ti sin garantia, particular")
    assert not verdict.excluded, f"wrongly excluded on {verdict.phrase!r} ({verdict.category})"


# ------------------------------------------------------------ phone rules
@pytest.mark.parametrize("title", [
    "Funda iPhone 15 Pro Max silicona",
    "Carcasa iPhone 16 transparente",
    "Pantalla para iPhone 15 original",
    "Cargador para iPhone 15 20W",
    "Cristal templado iPhone 17 Pro",
    "Replica iPhone 15 1:1",
    "Cambio de pantalla iPhone 15 en 30 minutos",
    "iPhone 16 Pro bloqueado por icloud",
])
def test_phone_accessories_and_services_are_excluded(title):
    """A "Funda iPhone 15" at 8 EUR against a 700 EUR reference is a 99% margin
    by the maths and pure junk in reality — the single biggest noise source on
    any phone search, and the listing db.log_junk's docstring blames for 2.86M
    audit rows."""
    verdict = junk.check(title)
    assert verdict.excluded
    assert verdict.category in ("NOT_A_PHONE", "DEFECT")


@pytest.mark.parametrize("title", [
    "iPhone 15 Pro Max 256GB impecable",
    "iPhone 15 con funda incluida",
    "iPhone 15 Pro Max, 100% original no replica",
    "iPhone 15 Plus 128GB con cargador y caja",
    "Apple iPhone 17 Pro Max 512GB precintado",
])
def test_real_phones_survive_the_accessory_rules(title):
    """The failure that matters. This file's founding rule is that a single word
    may never exclude: "iPhone 15 con funda incluida" is a phone sold *with* a
    case, and banning the bare word "funda" rejects a working listing for
    describing an extra — invisibly, because exclusions are never alerted on.
    So accessory nouns only exclude from the front of a title."""
    assert not junk.check(title).excluded


def test_phone_rules_do_not_touch_graphics_cards():
    """The phone branch is gated on the title naming an iPhone, so the accessory
    vocabulary cannot reach a GPU listing. "Pantalla" and "cargador" are the
    clearest cases: both are ordinary words in a card listing and both are
    junk-triggering at the front of a phone one.

    (Unrelated and pre-existing: "soporte vertical" is a GPU NOT_A_CARD phrase
    that fires anywhere in a title, so a card sold *with* a stand is excluded —
    the same shape as the "funda" problem fixed for phones above. Left alone
    deliberately; changing the GPU rules is not what this change is about.)
    """
    assert not junk.check("RTX 4070 Gigabyte, incluye cargador y cable").excluded
    assert not junk.check("Grafica RTX 4080 para pantalla 4K").excluded
    assert junk.check("Cargador para iPhone 15").excluded
    assert junk.check("Pantalla para iPhone 15").excluded


def test_gpu_rules_still_fire_after_the_phone_branch():
    """Guards the ordering: the phone check returns early, so the bundle and
    laptop rules must still be reachable for everything else."""
    assert junk.check("PC gaming con RTX 4070").category == "BUNDLE"
    assert junk.check("Portatil MSI RTX 4060").category == "LAPTOP"


# --------------------------------------------------------------------- consoles
# Every title below is real, sampled from Wallapop on 2026-08-26 by searching
# the same keywords the comps searches use. The split between the two lists is
# the whole reason the console rules exist: roughly four in five results
# carrying a console's name are a game or an accessory, and a game is a real
# listing at a real price that really sells — so nothing downstream would ever
# notice it dragging a console's reference toward 70 EUR.

REAL_CONSOLES = [
    "Ps5 pro 2tb blanca sin caja",
    "PS5 Pro 2TB",
    "Play 5 edicion digital + mando dualsense",
    "Consola PS5 con mando y cables",
    "Consola PlayStation 5 Blanca",
    "Playstation 5",
    "PS5 Sony como nueva",
    "PS5 Slim 1TB de disco",
    "PS5+dos controladores+juego+auriculares",
    "Consola PS5 Lector de Disco +Rdr2",
    "Xbox series x",
    "Xbox Series X + 2 mandos nuevos",
    "Consola Xbox Series X + 2 Mandos y Juegos",
    "Xbox Series X con caja",
    "Xbox Series X 1TB SSD con 2 mandos",
    "Xbox Series X 1TB + Mando Elite series 2",
    "Xbox Series X + Forza Horizon 5 Pack",
]

NOT_CONSOLES = [
    "Elden Ring PS5",
    "Ghost of Yotei PS5 Juego",
    "EA Sports FC 26 PS5",
    "Call of Duty Modern Warfare II para PS5",
    "Skylanders Trap Team PlayStation 4 PS4/PS5",
    "Hogwarts Legacy Xbox Series X",
    "FIFA 23 para Xbox Series X",
    "Resident Evil 4 Xbox Series X",
    "Avatar Frontiers of Pandora Xbox Series X",
    "the crew motorfest edition limited ps5",
    "Mando Xbox Series X",
    "Mando DualSense PS5",
    "Mando PS5 - Reparación de DRIFT",
    "Base de carga para Mandos PS5 Gaming",
    "Auriculares Sony Pulse 3D (PS5)",
    "Soporte Mando PS5 Batman Cable Guy",
    "Logo PS5",
    "Nevera Consola XBOX Series X 10L",
    "Dying Light 2 Stay Human Xbox Series X/S",
    "Crash Bandicoot 4 Xbox One/Series X",
    "Watch Dogs Legion Xbox One / Series X",
    "Pack 3 juegos xbox serie X.",
    "Auricular Ps4 compatible ps5, xbox series x/s, xbox one",
    "Mando Xbox Anti Drift Series X Original",
    "Sony A80J OLED 55 4K 120Hz HDMI 2.1 PS5",
]


def _priced_as_console(title):
    """What the pipeline actually does: junk first, then classify."""
    if junk.check(title).excluded:
        return None
    match = models.classify(title)
    if match.priceable and match.family == "console":
        return match.model_key
    return None


@pytest.mark.parametrize("title", REAL_CONSOLES)
def test_a_real_console_reaches_the_comps_pool(title):
    assert _priced_as_console(title) is not None, title


@pytest.mark.parametrize("title", NOT_CONSOLES)
def test_a_game_or_accessory_never_reaches_the_comps_pool(title):
    """The expensive failure. A 70 EUR game admitted to the PS5 pool looks
    entirely normal to every downstream check — it is a real price someone
    really paid — and there is no way to notice it afterwards."""
    assert _priced_as_console(title) is None, title


@pytest.mark.parametrize("title,expected", [
    ("Ps5 pro 2tb blanca sin caja", "ps5_pro"),
    ("PS5 Pro 2TB", "ps5_pro"),
    ("Play 5 edicion digital + mando dualsense", "ps5_digital"),
    ("PS5 Slim Edicion Digital 1TB", "ps5_digital"),
    ("Consola PS5 con mando y cables", "ps5"),
    ("PS5 Slim 1TB de disco", "ps5"),
    ("PS 5 con dos mandos", "ps5"),
    ("Play 5 con caja", "ps5"),
    ("Xbox series x", "xbox_series_x"),
    ("Consola Xbox Serie X", "xbox_series_x"),
])
def test_console_skus_are_told_apart(title, expected):
    """A Pro and a Digital are a couple of hundred euros apart; a median over
    both describes neither."""
    assert _priced_as_console(title) == expected


@pytest.mark.parametrize("title", [
    "Xbox Series S 512GB",          # the S is not the X
    "Consola Xbox Series S",
    "Xbox One X 1TB",               # One X is not Series X
    "Consola Xbox One X",
    "PS4 Pro 1TB",                  # PS4 Pro is not PS5 Pro
    "Consola PS4 Slim",
    "Consola PlayStation 4",
    "Consola PS3 Super Slim",
    "PSP Street",
    "Nintendo Switch OLED",
])
def test_a_neighbouring_console_never_maps_to_a_tracked_one(title):
    """The model numbers here collide by design — Sony and Microsoft both count
    upward and both sell a 'Pro' and an 'X'."""
    assert _priced_as_console(title) is None, title


def test_consoles_carry_the_console_family_and_may_alert():
    """Consoles alert as of 2026-09-08, on the owner's instruction.

    This test used to assert the opposite, and it is kept rather than deleted
    because the classification half of it is what actually matters: whatever
    the alerting policy is, a console must classify as family 'console'. That
    is what routes it to the console junk rules, to the console comps floor,
    and to whichever side of ALERTING_FAMILIES the owner has chosen.
    """
    import alert_loop
    for title in REAL_CONSOLES:
        match = models.classify(title)
        assert match.family == "console"
        assert match.family in alert_loop.ALERTING_FAMILIES


def test_phones_still_cannot_alert():
    """The console reversal must not have taken the phones with it.

    iPhones are tracked as a price-learning experiment the owner asked for
    without alerts. Enabling one comps-only family is exactly the change that
    could enable the other by accident, so this pins the distinction.
    """
    import alert_loop
    for title in ("iPhone 15 Pro Max 256GB", "iPhone 17 Pro 1TB"):
        match = models.classify(title)
        assert match.family == "phone"
        assert match.family not in alert_loop.ALERTING_FAMILIES


# A handful of games lead with the platform exactly the way a console listing
# does. No wording rule separates "PS5 Spiderman 2" from "PS5 Sony como nueva",
# so the pool floor is what has to catch them — all four titles below are real,
# sampled live on 2026-08-26 at the prices given.
GAMES_THAT_LOOK_LIKE_CONSOLES = [
    ("PS5 Necrophosis Full Consciousness", 25.0),
    ("PS5 Assassin's Creed Valhalla", 15.0),
    ("PS5 Spiderman 2", 38.0),
    ("PS5 One Piece Odyssey", 12.0),
    ("PS5 Elden Ring Nightreign edicion nueva", 69.99),  # a sealed new release
]


@pytest.mark.parametrize("title,price", GAMES_THAT_LOOK_LIKE_CONSOLES)
def test_a_game_titled_like_a_console_is_kept_out_by_the_pool_floor(title, price):
    """These do classify as consoles, deliberately — the classifier has no
    price and there is no wording that separates them. config's per-family
    floor is the gate, and this pins the two working together."""
    import config
    import pricing

    floor = config.MIN_COMP_PRICE_BY_FAMILY["console"]
    assert not pricing.comp_sane(price, floor), f"{title} at {price} must not enter the pool"


@pytest.mark.parametrize("price", [300.0, 360.0, 450.0, 780.0])
def test_a_real_console_price_still_enters_the_pool(price):
    """The counterpart, so the floor cannot pass by excluding everything."""
    import config
    import pricing

    floor = config.MIN_COMP_PRICE_BY_FAMILY["console"]
    assert pricing.comp_sane(price, floor)


def test_the_console_floor_sits_between_a_new_game_and_a_beaten_console():
    import config

    floor = config.MIN_COMP_PRICE_BY_FAMILY["console"]
    assert 80 < floor < 250, "floor must clear a sealed game and stay under any real console"
