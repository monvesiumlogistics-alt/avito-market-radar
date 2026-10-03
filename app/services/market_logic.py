"""Чистая логика проверки рынка: возраст, группы, отбор карточек, фильтры, порядок обхода. Без I/O."""

from collections.abc import Mapping, Sequence
from datetime import datetime
from itertools import groupby
from math import inf

from app.config import Settings
from app.db import Category
from app.models import Listing
from app.services.matcher import normalize

STOP_WORDS = {"новый", "новая", "новое", "новые", "оригинал", "original", "new", "шт", "комплект"}
GROUP_PRICE_SPREAD = 1.2  # копии одного товара: цена до +20 % от самой дешёвой в группе


def age_days(published_at: datetime, now: datetime) -> float:
    """Возраст в днях, не меньше 1 (знаменатель vpd)."""
    return max((now - published_at).total_seconds() / 86400, 1.0)


def find_age(page_date: datetime | None, search_date: datetime | None, now: datetime) -> float | None:
    """Возраст для vpd: дата со страницы карточки, дата выдачи — запасной вариант (m3)."""
    date = page_date or search_date
    return age_days(date, now) if date else None


def calc_vpd(views: int, age: float) -> int:
    return round(views / age)


def date_checked(seller_date: datetime | None, check_seller: bool) -> bool:
    """Отключённая проверка профиля: дата карточки и есть настоящая, пометка «не проверена» не нужна (m4)."""
    return seller_date is not None or not check_seller


def norm_title(title: str) -> str:
    """'Pioneer XDJ-RX3 новый' -> 'pioneer xdj rx3'; модели и цифры сохраняются."""
    return " ".join(w for w in normalize(title).split() if w not in STOP_WORDS)


def group_cards(cards: Sequence[Listing]) -> list[list[Listing]]:
    """Одинаковый norm_title; внутри — по цене, новая группа при цене > 1.2 × минимальной в группе."""
    groups: list[list[Listing]] = []
    priced = sorted((c for c in cards if c.price), key=lambda c: (norm_title(c.title), c.price))
    for _, same in groupby(priced, key=lambda c: norm_title(c.title)):
        current: list[Listing] = []
        for c in same:
            if current and c.price > current[0].price * GROUP_PRICE_SPREAD:
                groups.append(current)
                current = []
            current.append(c)
        groups.append(current)
    return groups


def _oldest_first(c: Listing) -> datetime:
    return c.published_at or datetime.max  # без даты — в конец


def pick_cards(groups: Sequence[Sequence[Listing]], per_group: int = 2) -> list[list[Listing]]:
    """Группы с самыми старыми карточками первыми, внутри — старшие первыми, не больше per_group.

    Слоты «по 2 на группу» не резервируются: вторую карточку краулер открывает условно и сам считает
    открытые страницы до REPORT_CARDS_PER_SUBCAT (m2).
    """
    ordered = [sorted(g, key=_oldest_first)[:per_group] for g in groups if g]
    return sorted(ordered, key=lambda g: _oldest_first(g[0]))


def is_find(vpd: int, price: int, age: float, s: Settings) -> bool:
    return vpd >= s.vpd_min and price >= s.min_price and age <= s.report_max_age_days


def is_hot(vpd: int, s: Settings) -> bool:
    return vpd >= s.vpd_hot


def _priority(cat: Category, now: datetime) -> float:
    if cat.last_crawled_at is None:
        return inf
    days = (now - cat.last_crawled_at).total_seconds() / 86400
    return days * (1 + min(cat.last_best_vpd or 0, 500) / 100)


def crawl_order(cats: Sequence[Category], run_id: int, now: datetime) -> list[Category]:
    """Непройденные в этом прогоне; разделы по лучшему приоритету подкатегорий, внутри раздела — по приоритету."""
    todo = sorted((c for c in cats if c.last_run_id != run_id), key=lambda c: (-_priority(c, now), c.id))
    best: dict[str, float] = {}
    for c in todo:  # todo по убыванию: первая подкатегория раздела — лучшая
        best.setdefault(c.section, _priority(c, now))
    rank = {sec: i for i, sec in enumerate(sorted(best, key=lambda sec: -best[sec]))}
    return sorted(todo, key=lambda c: rank[c.section])  # sorted стабилен: внутри раздела порядок по приоритету


def sort_finds(finds: Sequence, seen: Mapping[str, datetime]) -> list:
    """🔥 первыми, затем новые перед «уже было», затем по vpd. finds: .hot .vpd .group_key; seen: group_key -> дата."""
    return sorted(finds, key=lambda f: (not f.hot, f.group_key in seen, -f.vpd))
