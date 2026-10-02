from datetime import datetime
from pathlib import Path

from app.models import parse_search_urls
from app.providers.avito_parser import (
    extract_id,
    is_blocked,
    normalize_url,
    parse_price,
    parse_published,
    parse_search_html,
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
