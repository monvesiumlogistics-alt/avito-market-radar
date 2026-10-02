"""Разбор HTML выдачи Avito. Селекторы проверены на живой выдаче 2026-10-02 (tests/fixtures/avito_search.html).

Если Avito поменял вёрстку — правь только SELECTORS (первый сработавший селектор побеждает).
"""

import hashlib
import re
from datetime import datetime, timedelta
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, Tag

from app.models import Listing

BASE_URL = "https://www.avito.ru"

SELECTORS: dict[str, list[str]] = {
    "card": ['[data-marker="item"]'],
    "title": ['[itemprop="name"]', '[data-marker="item-title"]'],
    "link": ['a[data-marker="item-title"]', 'a[itemprop="url"]'],
    "price": ['meta[itemprop="price"]', '[data-marker="item-price-value"]', '[data-marker="item-price"]'],
    "image": ['img[itemprop="image"]', '[data-marker="item-photo"] img'],
    "location": ['[data-marker="item-address"]', '[data-marker="item-location"]'],
    "date": ['[data-marker="item-date"]'],
    "description": ['meta[itemprop="description"]', '[class*="item-description"]'],
    "seller": ['a[href*="/user/"] p', 'a[href*="/brands/"] p'],
}

BLOCK_MARKERS = ("Доступ ограничен", 'class="firewall-container', "firewall-title")


def is_blocked(html: str, title: str = "") -> bool:
    return "Доступ ограничен" in title or any(m in html for m in BLOCK_MARKERS)


def parse_price(text: str | None) -> int | None:
    if not text:
        return None
    if "бесплатно" in text.lower():
        return 0
    digits = re.sub(r"\D", "", text)
    return int(digits) if digits else None


def normalize_url(href: str, base: str = BASE_URL) -> str:
    parts = urlsplit(urljoin(base, href))
    return urlunsplit(("https", parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def extract_id(data_item_id: str | None, url: str) -> str:
    if data_item_id and data_item_id.isdigit():
        return data_item_id
    m = re.search(r"_(\d{6,})$", urlsplit(url).path)
    if m:
        return m.group(1)
    return hashlib.sha1(normalize_url(url).encode()).hexdigest()[:16]


def city_from_url(url: str) -> str | None:
    """На карточке выдачи город пустой; он есть первым сегментом пути: /moskva/odezhda/..."""
    parts = [p for p in urlsplit(url).path.split("/") if p]
    return parts[0] if len(parts) >= 2 else None


_UNITS = (("мин", "minutes"), ("час", "hours"), ("недел", "weeks"), ("д", "days"))


def parse_published(text: str | None, now: datetime | None = None) -> datetime | None:
    """'5 минут назад', 'час назад', '2 дня назад', 'Сегодня 14:30', 'Вчера 09:05'. Иначе None."""
    if not text:
        return None
    now = now or datetime.now()
    t = text.lower().strip()
    if "только что" in t or "секунд" in t:
        return now
    m = re.search(r"(\d+)?\s*(минут\w*|час\w*|дн\w*|день|недел\w*)\s+назад", t)
    if m:
        n = int(m.group(1) or 1)
        for prefix, unit in _UNITS:
            if m.group(2).startswith(prefix):
                return now - timedelta(**{unit: n})
    m = re.search(r"(сегодня|вчера)\D*(\d{1,2}):(\d{2})", t)
    if m:
        day = now if m.group(1) == "сегодня" else now - timedelta(days=1)
        return day.replace(hour=int(m.group(2)), minute=int(m.group(3)), second=0, microsecond=0)
    return None


def with_page(url: str, page: int) -> str:
    if page <= 1:
        return url
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if k != "p"] + [("p", str(page))]
    return urlunsplit(parts._replace(query=urlencode(query)))


def _first(card: Tag, key: str) -> Tag | None:
    for selector in SELECTORS[key]:
        el = card.select_one(selector)
        if el is not None:
            return el
    return None


def _value(el: Tag | None) -> str | None:
    if el is None:
        return None
    value = el.get("content") if el.name == "meta" else el.get_text(" ", strip=True)
    return value or None


def _image(card: Tag) -> str | None:
    img = _first(card, "image")
    src = img and (img.get("src") or img.get("data-src"))
    if img is not None and not src and img.get("srcset"):
        src = img["srcset"].split(",")[-1].strip().split(" ")[0]  # самый крупный вариант
    if not src:
        # картинки вне экрана грузятся лениво, но URL есть в маркере слайдера
        slide = card.select_one('[data-marker^="slider-image/image-"]')
        src = slide and slide["data-marker"].removeprefix("slider-image/image-")
    return src or None


def parse_search_html(html: str, label: str = "") -> list[Listing]:
    soup = BeautifulSoup(html, "html.parser")
    listings = []
    for card in soup.select(SELECTORS["card"][0]):
        link = _first(card, "link")
        title = _value(_first(card, "title"))
        if link is None or not link.get("href") or not title:
            continue
        url = normalize_url(link["href"])
        listings.append(
            Listing(
                external_id=extract_id(card.get("data-item-id"), url),
                title=title,
                description=_value(_first(card, "description")),
                price=parse_price(_value(_first(card, "price"))),
                url=url,
                image_url=_image(card),
                location=_value(_first(card, "location")) or city_from_url(url),
                seller_name=_value(_first(card, "seller")),
                category=label or None,
                published_at=parse_published(_value(_first(card, "date"))),
            )
        )
    return listings
