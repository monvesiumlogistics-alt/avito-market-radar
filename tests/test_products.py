"""ADR-022: Product identity — заголовок → бренд + модель → канонический ключ.
Ложное слияние хуже ложного разделения."""

import json
from pathlib import Path

import pytest

from app.services.market_logic import goofish_url
from app.services.products import PRODUCT_EXTRACTOR_VERSION, identify, normalize

CORPUS = json.loads((Path(__file__).parent / "fixtures" / "product_titles.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("case", CORPUS, ids=[c["title"][:40] for c in CORPUS])
def test_corpus(case):
    """~160 заголовков: реальные из базы (source=db) и составленные по реальным форматам (synthetic)."""
    got = identify(case["title"], case["domain"])
    assert (got.canonical_key, got.confidence) == (case["canonical_key"], case["confidence"])
    assert got.clusterable == (case["confidence"] in ("HIGH", "MEDIUM"))


def test_corpus_shape():
    assert len(CORPUS) >= 150 and sum(c["source"] == "db" for c in CORPUS) >= 90
    assert {c["domain"] for c in CORPUS} >= {"ebike", "bike", "laptop", "watch", "phone", "optics", "pc", "fashion"}


def key(title, domain="generic"):
    return identify(title, domain).canonical_key


@pytest.mark.parametrize(
    ("titles", "domain"),
    [
        (["Pioneer DDJ-FLX4", "Pioneer DDJ FLX4", "Pioneer DJ DDJ-FLX4", "Контроллер DDJ-FLX4 Pioneer"], "generic"),
        (["Sony Alpha A7 IV", "Sony A7IV", "Sony a7 4"], "generic"),
        (["Insta 360 x3", "Insta360 X3", "insta360 x3"], "generic"),
        (["Honor MagicBook X 16", "Honor Magicbook X16 i5-13420h 16/512Гб", "Ноутбук honor MagicBook X16"], "laptop"),
        (["RTX 4070 Ti", "RTX 4070ti 12gb", "MSI RTX 4070 Ti Gaming X"], "pc"),
        (["Aimiko U2 Pro", "Aimiko u2 pro 48V 20Ah", "Электровелосипед Aimiko U2 Pro черный"], "ebike"),
        (["Fujifilm X-T5", "Fujifilm XT5"], "generic"),
    ],
)
def test_same_product(titles, domain):
    keys = {key(t, domain) for t in titles}
    assert len(keys) == 1 and None not in keys


@pytest.mark.parametrize(
    ("titles", "domain"),
    [
        (["Pioneer DDJ-FLX4", "Pioneer DDJ-FLX10"], "generic"),
        (["DJI Mini 3", "DJI Mini 3 Pro", "DJI Mini 4 Pro", "DJI Avata 2"], "generic"),
        (["Sony A7 III", "Sony A7 IV"], "generic"),
        (["Fujifilm X100V", "Fujifilm X100VI"], "generic"),
        (["Lenovo Legion 5", "Lenovo Legion 5 Pro", "Lenovo Legion 7", "Lenovo Legion Go"], "laptop"),
        (["RTX 4070", "RTX 4070 Ti", "RTX 4070 Super", "RTX 4090", "RTX 4090 Super"], "pc"),
        (["Aimiko U2", "Aimiko U2 Pro"], "ebike"),
        (["iPhone 15 Pro", "iPhone 15 Pro Max", "iPhone 15"], "phone"),
        (["Levenhuk Sherman Pro 12x50", "Levenhuk Sherman Pro 8x42"], "optics"),
    ],
)
def test_different_products(titles, domain):
    keys = [key(t, domain) for t in titles]
    assert None not in keys and len(set(keys)) == len(keys)


def test_brand_only_never_becomes_a_product():
    for title in ("Stone Island jacket", "Брюки карго Stone Island", "Lenovo Legion gaming laptop", "Ноутбук Honor"):
        got = identify(title, "fashion" if "Stone" in title else "laptop")
        assert got.confidence == "LOW" and got.canonical_key is None and got.brand


def test_unknown_and_noise():
    assert identify("Игровой ноутбук отличный новый", "laptop").confidence == "UNKNOWN"
    noisy = identify("🔥НОВИНКА🔥 Pioneer оригинал DDJ-FLX4 в наличии")
    assert (noisy.display_name, noisy.confidence) == ("Pioneer DDJ-FLX4", "HIGH")


def test_variant_is_not_identity():
    phone = identify("iPhone 15 Pro, 256 ГБ, SIM + eSIM", "phone")
    assert phone.canonical_key == "apple|iphone-15-pro" and phone.display_name == "Apple iPhone 15 Pro"
    ebike = identify("Aimiko U2 Pro 48V 20Ah черный", "ebike")
    assert ebike.canonical_key == "aimiko|u2-pro" and ebike.variant.startswith("48v 20ah")  # вариант не в ключе
    laptop = identify("Lenovo Legion 5 Pro 16IRX9 32/1tb", "laptop")  # тип машины — часть ключа ноутбука
    assert laptop.canonical_key == "lenovo|legion-5-pro|16irx9" and laptop.display_name == "Lenovo Legion 5 Pro"


def test_normalize_keeps_model_meaning():
    assert normalize("A7 III") != normalize("A7 IV")
    assert normalize("Arc’teryx Cervélo kugoо 12х45 c:62") == ["arc'teryx", "cervelo", "kugoo", "12x45", "c62"]


def test_goofish_query_uses_product_identity():
    url = goofish_url("Pioneer DJ контроллер DDJ FLX4 оригинал новый")
    assert url == "https://www.goofish.com/search?q=Pioneer+DDJ-FLX4"
    assert goofish_url("Коляска детская") is None  # нет модели и нет латиницы — как раньше
    assert "Weltmeister" in goofish_url("Баян Weltmeister 120")  # бренд не в реестре — старый fallback


def test_extractor_version():
    assert PRODUCT_EXTRACTOR_VERSION == 1
