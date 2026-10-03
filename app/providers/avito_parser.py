"""Разбор HTML выдачи Avito. Селекторы проверены на живой выдаче 2026-10-02 (tests/fixtures/avito_search.html).

Если Avito поменял вёрстку — правь только SELECTORS (первый сработавший селектор побеждает).
"""

import hashlib
import re
from dataclasses import dataclass
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
    # проверено на живых страницах 2026-10-03 (ADR-004)
    "subcat": ['[data-marker="rubricator"] a[data-marker$="/clickable"]'],
    "item_date": ['[data-marker="item-view/item-date"]'],
    "item_views": ['[data-marker="item-view/total-views"]'],
    "item_today": ['[data-marker="item-view/today-views"]'],
    "item_seller_link": ['[data-marker="seller-link/link"]', 'a[href*="/user/"]', 'a[href*="/brands/"]'],
    "profile_item": ['[data-marker^="item_list_with_filters/item("]'],
}

# Тексты вёрстки в одном месте (NFR-6)
TEXT_PATTERNS = {
    "promoted": re.compile(r"Продвинуто|Забронировано"),
    "views": re.compile(r"(\d[\d\s]*)\s*просмотр"),
    "today": re.compile(r"\+\s*(\d[\d\s]*)\s*сегодня"),
}

BLOCK_MARKERS = ("Доступ ограничен", 'class="firewall-container', "firewall-title", "Вы робот")


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


_MONTHS = {
    m: i
    for i, m in enumerate(
        (
            "января",
            "февраля",
            "марта",
            "апреля",
            "мая",
            "июня",
            "июля",
            "августа",
            "сентября",
            "октября",
            "ноября",
            "декабря",
        ),
        1,
    )
}


def parse_published(text: str | None, now: datetime | None = None, absolute_time: bool = False) -> datetime | None:
    """'5 минут назад', 'час назад', '2 дня назад', 'Сегодня 14:30', 'Вчера 09:05', 'вчера' (день), '25 сентября'.

    Недели/месяцы назад и даты без времени ('25 сентября [2025]') разбираются всегда; 'D месяц HH:MM'
    (профиль продавца, карточка) только с absolute_time=True — по умолчанию None, как раньше. Иначе None.
    """
    if not text:
        return None
    now = now or datetime.now()
    t = text.lower().strip()
    if "только что" in t or "секунд" in t:
        return now
    m = re.search(r"(\d+)?\s*месяц\w*\s+назад", t)
    if m:
        return now - timedelta(days=30 * int(m.group(1) or 1))  # заведомо «старое»
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
    m = re.search(r"(\d{1,2})\s+([а-я]+)(?:\s+(\d{4}))?(?:\s+(?:в\s+)?(\d{1,2}):(\d{2}))?", t)
    if m and m.group(2) in _MONTHS and (absolute_time or not m.group(4)):
        try:
            d = datetime(
                int(m.group(3) or now.year),
                _MONTHS[m.group(2)],
                int(m.group(1)),
                int(m.group(4) or 0),
                int(m.group(5) or 0),
            )
            if d > now and not m.group(3):
                d = d.replace(year=d.year - 1)  # «25 декабря» в январе — прошлый год
        except ValueError:
            return None
        return d
    m = re.fullmatch(r"(сегодня|вчера)", t)  # поиск показывает только день, без времени
    if m:
        return now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=m.group(1) == "вчера")
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


def parse_search_html(html: str, label: str = "", now: datetime | None = None) -> list[Listing]:
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
                published_at=parse_published(_value(_first(card, "date")), now),
            )
        )
    return listings


@dataclass(frozen=True)
class ItemStats:
    views: int | None = None
    today: int | None = None
    date_text: str | None = None  # '· вчера в 22:36' без «· »; разбирать parse_published(absolute_time=True)
    seller_url: str | None = None


def _digits(pattern: str, text: str) -> int | None:
    m = TEXT_PATTERNS[pattern].search(text)
    return int(re.sub(r"\D", "", m.group(1))) if m else None


def parse_item_page(html: str) -> ItemStats:
    """Страница карточки: просмотры, «+сегодня», дата, ссылка на продавца. Чего нет — None."""
    soup = BeautifulSoup(html, "html.parser")
    views = _value(_first(soup, "item_views"))
    today = _value(_first(soup, "item_today"))
    date = _value(_first(soup, "item_date"))
    link = _first(soup, "item_seller_link")
    return ItemStats(
        views=_digits("views", views) if views else None,
        today=_digits("today", today) if today else None,
        date_text=date.lstrip("· ").strip() or None if date else None,
        seller_url=normalize_url(link["href"]) if link is not None and link.get("href") else None,
    )


def parse_seller_date(html: str, item_id: str) -> str | None:
    """Дата объявления item_id в профиле продавца (текст как есть, '5 часов назад') или None."""
    soup = BeautifulSoup(html, "html.parser")
    for card in soup.select(SELECTORS["profile_item"][0]):
        if card.get("data-item-id") == item_id:
            return _value(_first(card, "date"))
    return None


def promoted_ids(html: str) -> set[str]:
    """id продвинутых/зарезервированных карточек выдачи: они не останавливают листание."""
    soup = BeautifulSoup(html, "html.parser")
    return {
        card["data-item-id"]
        for card in soup.select(SELECTORS["card"][0])
        if card.get("data-item-id") and TEXT_PATTERNS["promoted"].search(card.get_text(" ", strip=True))
    }


def parse_subcategories(html: str, section: str, limit: int) -> list[tuple[str, str]]:
    """Подкатегории со страницы раздела: [(название, url)], не больше limit. Ссылки /all/<section>/<sub>-<hash>.

    Если рубрикатора нет (у ноутбуков, ремонта, мебели там бренды/типы в другой вёрстке) — запасной путь из
    catalog.js: любые ссылки /<x>/<section>/<sub>, не объявления, текст короче 40, адрес приводится к /rossiya/.
    """
    soup = BeautifulSoup(html, "html.parser")
    seen: dict[str, str] = {}
    for a in soup.select(SELECTORS["subcat"][0]):
        parts = [p for p in urlsplit(a.get("href", "")).path.split("/") if p]
        name = a.get_text(" ", strip=True)
        if len(parts) == 3 and parts[1] == section and name:
            seen.setdefault(normalize_url(a["href"]), name)
    if not seen:
        for a in soup.find_all("a", href=True):
            parts = [p for p in urlsplit(a["href"]).path.split("/") if p]
            name = a.get_text(" ", strip=True)
            if (
                len(parts) == 3
                and parts[1] == section
                and re.fullmatch(r"[a-z_-]+", parts[0])
                and not re.search(r"_\d{7,}$", parts[2])
                and name
                and len(name) < 40
            ):
                seen.setdefault(normalize_url(f"/rossiya/{section}/{parts[2]}"), name)
    return [(name, url) for url, name in list(seen.items())[:limit]]
