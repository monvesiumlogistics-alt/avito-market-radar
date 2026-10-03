from datetime import datetime, timedelta
from types import SimpleNamespace as NS

from app.config import Settings
from app.models import Listing
from app.services.market_logic import (
    age_days,
    crawl_order,
    date_checked,
    find_age,
    group_cards,
    is_find,
    is_hot,
    norm_title,
    pick_cards,
    sort_finds,
)

NOW = datetime(2026, 10, 3, 12, 0)
S = Settings(_env_file=None)


def card(i: str, title: str, price: int, days: float | None = 3) -> Listing:
    published = NOW - timedelta(days=days) if days is not None else None
    return Listing(external_id=i, title=title, price=price, url=f"https://www.avito.ru/x_{i}", published_at=published)


def test_age_days_min_one():
    assert age_days(NOW - timedelta(hours=3), NOW) == 1.0
    assert age_days(NOW - timedelta(days=3), NOW) == 3.0


def test_find_age_page_date_wins_search_fallback():
    page, search = NOW - timedelta(days=4), NOW - timedelta(days=1)
    assert find_age(page, search, NOW) == 4.0  # m3
    assert find_age(None, search, NOW) == 1.0
    assert find_age(None, None, NOW) is None


def test_date_checked_when_seller_check_disabled():
    assert date_checked(None, check_seller=False)  # m4
    assert not date_checked(None, check_seller=True)
    assert date_checked(NOW, check_seller=True)


def test_norm_title():
    assert norm_title("Pioneer XDJ-RX3 новый") == norm_title("pioneer xdj rx3 ОРИГИНАЛ") == "pioneer xdj rx3"
    assert norm_title("Pioneer XDJ-RX2") != norm_title("Pioneer XDJ-RX3")


def test_group_cards_price_spread():
    cards = [
        card("1", "iPhone 15", 50000),
        card("2", "iphone 15 новый", 58000),
        card("3", "iPhone 15", 61000),
        card("4", "iPhone 14", 40000),
        card("5", "Без цены", 0),
    ]
    ids = sorted(sorted(c.external_id for c in g) for g in group_cards(cards))
    assert ids == [["1", "2"], ["3"], ["4"]]  # 61000 > 50000 * 1.2, без цены отброшена


def test_pick_cards_oldest_groups_and_cards_first():
    g_young = [card("1", "a", 10000, 2), card("2", "a", 10000, 3), card("3", "a", 10000, 1)]
    g_old = [card("4", "b", 10000, 6)]
    g_undated = [card("5", "c", 10000, None)]
    picked = pick_cards([g_young, g_undated, g_old])
    assert [[c.external_id for c in g] for g in picked] == [["4"], ["2", "1"], ["5"]]


def test_pick_cards_no_slot_reservation_m2():
    groups = [[card(str(i), f"t{i}", 10000, 2), card(f"{i}b", f"t{i}", 10000, 1)] for i in range(20)]
    picked = pick_cards(groups)
    assert len(picked) == 20 and all(len(g) == 2 for g in picked)  # кап 12 считает краулер


def test_is_find_is_hot():
    assert is_find(50, 10000, 7, S) and is_hot(100, S) and not is_hot(99, S)
    assert not is_find(49, 10000, 3, S)
    assert not is_find(80, 9999, 3, S)
    assert not is_find(80, 10000, 7.5, S)


def cat(i: int, section: str, crawled_days: float | None = None, vpd: int | None = None, run: int | None = None):
    crawled = NOW - timedelta(days=crawled_days) if crawled_days is not None else None
    return NS(id=i, section=section, last_run_id=run, last_best_vpd=vpd, last_crawled_at=crawled)


def test_crawl_order():
    cats = [
        cat(1, "A", 10),  # приоритет 10
        cat(2, "A", 2, vpd=500),  # 2 * 6 = 12
        cat(3, "B", 5),  # 5
        cat(4, "B", None),  # ни разу не обходили -> первым
        cat(5, "C", 30, run=7),  # пройдена в этом прогоне -> исключена
        cat(6, "C", 40, run=6),  # пройдена в другом прогоне -> 40
    ]
    # разделы: B (inf), C (40), A (12); внутри раздела по приоритету
    assert [c.id for c in crawl_order(cats, run_id=7, now=NOW)] == [4, 3, 6, 2, 1]


def test_sort_finds_hot_then_new_then_vpd():
    f = [
        NS(group_key="a", hot=False, vpd=90),
        NS(group_key="b", hot=True, vpd=150),
        NS(group_key="c", hot=True, vpd=300),
        NS(group_key="d", hot=False, vpd=60),
    ]
    seen = {"c": NOW, "d": NOW}
    assert [x.group_key for x in sort_finds(f, seen)] == ["b", "c", "a", "d"]
