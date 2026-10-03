from datetime import datetime
from pathlib import Path

from app.models import parse_search_urls
from app.providers.avito_parser import (
    extract_id,
    is_blocked,
    normalize_url,
    parse_item_page,
    parse_price,
    parse_published,
    parse_search_html,
    parse_seller_date,
    parse_subcategories,
    promoted_ids,
    with_page,
)

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_price():
    assert parse_price("1 500 ₽") == 1500
    assert parse_price("1 500 ₽") == 1500
    assert parse_price("1500") == 1500
    assert parse_price("Бесплатно") == 0
    assert parse_price("Цена не указана") is None
    assert parse_price(None) is None


def test_normalize_url_drops_query_and_fragment():
    assert (
        normalize_url("/moskva/odezhda/kurtka_123456789?context=abc#x")
        == "https://www.avito.ru/moskva/odezhda/kurtka_123456789"
    )
    assert normalize_url("https://WWW.avito.ru/a/b/") == "https://www.avito.ru/a/b"


def test_extract_id():
    assert extract_id("8511286466", "https://www.avito.ru/x/y/z_1") == "8511286466"
    assert extract_id(None, "https://www.avito.ru/perm/odezhda/remen_8511286466") == "8511286466"
    fallback = extract_id(None, "https://www.avito.ru/perm/odezhda/remen?x=1")
    assert len(fallback) == 16 and fallback == extract_id("", "https://www.avito.ru/perm/odezhda/remen")


def test_parse_published():
    now = datetime(2026, 10, 2, 15, 0)
    assert parse_published("1 час назад", now) == datetime(2026, 10, 2, 14, 0)
    assert parse_published("час назад", now) == datetime(2026, 10, 2, 14, 0)
    assert parse_published("15 минут назад", now) == datetime(2026, 10, 2, 14, 45)
    assert parse_published("2 дня назад", now) == datetime(2026, 9, 30, 15, 0)
    assert parse_published("Вчера 09:05", now) == datetime(2026, 10, 1, 9, 5)
    assert parse_published("1 октября 12:00", now) is None


def test_with_page():
    assert with_page("https://www.avito.ru/all?q=a&s=104", 1) == "https://www.avito.ru/all?q=a&s=104"
    assert with_page("https://www.avito.ru/all?q=a&p=2", 3) == "https://www.avito.ru/all?q=a&p=3"


def test_parse_search_urls():
    urls = parse_search_urls("Мужское|https://a.ru/1;https://a.ru/2; ")
    assert [(u.label, u.url) for u in urls] == [("Мужское", "https://a.ru/1"), ("", "https://a.ru/2")]


def test_parser_on_real_fixture():
    listings = parse_search_html((FIXTURES / "avito_search.html").read_text(encoding="utf-8"), "Одежда")
    assert len(listings) == 4
    first = listings[0]
    assert first.external_id == "8350489109"
    assert "Dolce Gabbana" in first.title
    assert first.price == 2500
    assert first.url.startswith("https://www.avito.ru/shahty/odezhda_obuv_aksessuary/")
    assert "?" not in first.url
    assert first.image_url and first.image_url.startswith("https://")
    assert first.location == "shahty"
    assert first.seller_name == "данил"
    assert first.category == "Одежда"
    assert first.published_at is not None
    assert all(item.external_id.isdigit() for item in listings)


def test_block_page_detected():
    html = (FIXTURES / "avito_blocked.html").read_text(encoding="utf-8")
    assert is_blocked(html)
    assert parse_search_html(html) == []
    assert not is_blocked((FIXTURES / "avito_search.html").read_text(encoding="utf-8"))


def _fx(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def test_parse_published_market_formats():
    now = datetime(2026, 10, 3, 15, 0)
    assert parse_published("Вчера", now) == datetime(2026, 10, 2)
    assert parse_published("Сегодня", now) == datetime(2026, 10, 3)
    assert parse_published("25 сентября", now) == datetime(2026, 9, 25)
    assert parse_published("25 сентября 2025", now) == datetime(2025, 9, 25)
    assert parse_published("25 декабря", now) == datetime(2025, 12, 25)  # будущая дата = прошлый год
    assert parse_published("1 неделю назад", now) == datetime(2026, 9, 26, 15, 0)
    assert parse_published("месяц назад", now) == datetime(2026, 9, 3, 15, 0)
    assert parse_published("3 месяца назад", now) == datetime(2026, 7, 5, 15, 0)
    assert parse_published("вчера в 22:36", now) == datetime(2026, 10, 2, 22, 36)
    assert parse_published("23 июня 12:08", now) is None  # со временем — только absolute_time
    assert parse_published("23 июня 12:08", now, absolute_time=True) == datetime(2026, 6, 23, 12, 8)
    assert parse_published("31 февраля", now) is None
    assert parse_published("мусор", now) is None


def test_block_marker_robot():
    assert is_blocked("<html>Вы робот?</html>")
    assert not any(is_blocked(_fx(n)) for n in FIXTURE_PAGES)


FIXTURE_PAGES = (
    "market_search_s104.html",
    "market_section.html",
    "market_item.html",
    "market_item_reserved.html",
    "market_seller.html",
)


def test_search_fixture_bare_yesterday_and_promoted():
    html = _fx("market_search_s104.html")
    listings = parse_search_html(html)
    assert len(listings) == 50
    assert sum(1 for x in listings if x.published_at is not None) >= 49
    promo = promoted_ids(html)
    assert {"8407318558", "8480160604", "8308156219", "8336568258"} <= promo
    assert promo <= {x.external_id for x in listings}
    assert promoted_ids("<html></html>") == set()


def test_parse_subcategories():
    html = _fx("market_section.html")
    subs = parse_subcategories(html, "muzykalnye_instrumenty", 18)
    assert len(subs) == 8
    name, url = subs[0]
    assert name == "Аккордеоны, гармони, баяны"
    assert url.startswith("https://www.avito.ru/all/muzykalnye_instrumenty/akkordeony_garmoni_bayany-")
    assert "?" not in url
    assert len(parse_subcategories(html, "muzykalnye_instrumenty", 3)) == 3
    assert parse_subcategories(html, "telefony", 18) == []
    assert parse_subcategories("<html></html>", "telefony", 18) == []


def test_parse_item_page():
    st = parse_item_page(_fx("market_item.html"))
    assert (st.views, st.today, st.date_text) == (31, 14, "вчера в 22:36")
    assert st.seller_url and st.seller_url.startswith("https://www.avito.ru/brands/")
    assert "?" not in st.seller_url
    st = parse_item_page(_fx("market_item_reserved.html"))
    assert (st.views, st.today, st.date_text, st.seller_url) == (72, 13, "вчера в 22:22", None)
    assert parse_item_page("<html></html>") == parse_item_page("") and parse_item_page("").views is None


def test_parse_seller_date():
    html = _fx("market_seller.html")
    assert parse_seller_date(html, "8427557978") == "5 часов назад"
    assert parse_seller_date(html, "8134124978") == "23 июня 12:08"
    assert parse_seller_date(html, "1") is None
    assert parse_seller_date("<html></html>", "8427557978") is None
