import asyncio
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db import Category, CrawlRun, Find, init_db
from app.providers.base import AvitoProvider, Page, ProviderBlocked
from app.services.market import BudgetExhausted, MarketCrawler, StopRequested

NOW = datetime(2026, 10, 3, 12, 0)
CAT_URL = "https://www.avito.ru/all/muzykalnye_instrumenty/akkordeony-ASgB"
FIXTURES = Path(__file__).parent / "fixtures"
THREE_DAYS = "30 сентября в 12:00"  # возраст 3 дня


def search_url(page: int = 1) -> str:
    return f"{CAT_URL}?s=104&pmin=10000" + (f"&p={page}" if page > 1 else "")


def card_url(i) -> str:
    return f"https://www.avito.ru/x/y/t_{i}"


def search_html(*cards: tuple) -> str:
    """cards: (id, title, price, date[, promo])"""
    out = []
    for i, title, price, date, *promo in cards:
        out.append(
            f'<div data-marker="item" data-item-id="{i}"><a data-marker="item-title" href="/x/y/t_{i}">'
            f'<h3 itemprop="name">{title}</h3></a><meta itemprop="price" content="{price}">'
            f'<p data-marker="item-date">{date}</p>{"<i>Забронировано</i>" if promo else ""}</div>'
        )
    return "<html><body>" + "".join(out) + "</body></html>"


def item_html(views: int, date: str = THREE_DAYS, today: int | None = 5, seller: bool = True) -> str:
    return (
        f'<span data-marker="item-view/item-date">· {date}</span>'
        f'<span data-marker="item-view/total-views">{views} просмотров</span>'
        + (f'<span data-marker="item-view/today-views">(+{today} сегодня)</span>' if today is not None else "")
        + ('<a data-marker="seller-link/link" href="https://www.avito.ru/brands/abc?x=1">s</a>' if seller else "")
    )


SELLER_URL = "https://www.avito.ru/brands/abc"


def seller_html(i, date: str) -> str:
    return (
        f'<div data-marker="item_list_with_filters/item(1)" data-item-id="{i}">'
        f'<p data-marker="item-date">{date}</p></div>'
    )


class FakeProvider(AvitoProvider):
    def __init__(self, pages: dict):
        self.pages, self.calls = pages, []

    async def search(self, search, page=1):
        raise AssertionError

    async def fetch(self, url, ready_selector=None):
        self.calls.append(url)
        res = "<html></html>" if "s=104" in url and url not in self.pages else self.pages[url]  # KeyError = ошибка
        if isinstance(res, BaseException):
            raise res
        if callable(res):
            res = res()
        return Page(res, "", url)


class FakeNotifier:
    async def send_text(self, text):
        return 1

    async def edit_text(self, message_id, text):
        pass


def setup(tmp_path, pages: dict, seed: bool = True, **settings):
    sf = init_db(f"sqlite:///{tmp_path}/t.db")
    s = Settings(_env_file=None, **settings)
    with sf() as db:
        cat = Category(section="muzykalnye_instrumenty", name="Аккордеоны", url=CAT_URL, discovered_at=NOW)
        run = CrawlRun(started_at=NOW)
        db.add_all([cat, run] if seed else [run])
        db.commit()
        cat_id, run_id = (cat.id if seed else None), run.id
    crawler = MarketCrawler(sf, lambda: None, FakeNotifier(), s, clock=lambda: NOW)
    crawler.provider, crawler.run_id = FakeProvider(pages), run_id
    return SimpleNamespace(sf=sf, crawler=crawler, cat_id=cat_id, run_id=run_id, provider=crawler.provider)


def rows(t, model=Find):
    with t.sf() as db:
        return db.scalars(select(model)).all()


def cat_row(t) -> Category:
    with t.sf() as db:
        return db.get(Category, t.cat_id)


def opened(t) -> list[str]:
    return [u for u in t.provider.calls if "/t_" in u]


# --- листание ---


async def test_crawl_stops_on_old(tmp_path):
    pages = {
        search_url(1): search_html(
            ("1", "Баян А", 20000, "2 дня назад"),
            ("2", "Баян Б", 20000, "10 дней назад"),  # старая: дочитываем страницу, дальше не листаем
            ("3", "Баян В", 20000, "1 день назад"),
        ),
        search_url(2): search_html(("4", "Баян Г", 20000, "1 день назад")),
        card_url(1): item_html(10),
        card_url(3): item_html(10),
    }
    t = setup(tmp_path, pages)
    assert await t.crawler.crawl_subcategory(t.cat_id)
    assert search_url(2) not in t.provider.calls
    assert sorted(opened(t)) == [card_url(1), card_url(3)]  # старая карточка не открывается (AC-2.4)
    assert cat_row(t).last_days_covered == 7.0


async def test_promo_no_stop(tmp_path):
    pages = {
        search_url(1): search_html(("1", "Баян А", 20000, "2 дня назад"), ("2", "Баян Б", 20000, "10 дней назад", 1)),
        search_url(2): search_html(("3", "Баян В", 20000, "1 день назад")),
        search_url(3): search_html(("4", "Баян Г", 20000, "10 дней назад")),
        card_url(1): item_html(10),
        card_url(3): item_html(10),
    }
    t = setup(tmp_path, pages)
    await t.crawler.crawl_subcategory(t.cat_id)
    assert search_url(2) in t.provider.calls and search_url(3) in t.provider.calls
    assert card_url(2) not in t.provider.calls


async def test_max_pages_and_days_covered_when_page_limit(tmp_path):
    pages = {
        search_url(1): search_html(("1", "Баян А", 20000, "1 день назад")),
        search_url(2): search_html(("2", "Баян Б", 20000, "3 дня назад")),
        search_url(3): search_html(("3", "Баян В", 20000, "5 дней назад")),
        card_url(1): item_html(10),
        card_url(2): item_html(10),
    }
    t = setup(tmp_path, pages, report_max_pages=2)
    await t.crawler.crawl_subcategory(t.cat_id)
    assert search_url(3) not in t.provider.calls
    assert cat_row(t).last_days_covered == pytest.approx(3.0)  # неделя не покрыта: «покрыто 3 дня из 7»


async def test_price_cut_local_with_pmin(tmp_path):
    pages = {
        search_url(1): search_html(("1", "Дешёвый", 5000, "1 день назад"), ("2", "Баян", 20000, "1 день назад")),
        card_url(2): item_html(10),
    }
    t = setup(tmp_path, pages)
    await t.crawler.crawl_subcategory(t.cat_id)
    assert "pmin=10000" in t.provider.calls[0]
    assert opened(t) == [card_url(2)]  # Avito мог проигнорировать pmin, локальный фильтр режет


async def test_old_age_cut_by_search_date(tmp_path):
    pages = {search_url(1): search_html(("1", "Баян", 20000, "8 дней назад"))}
    t = setup(tmp_path, pages)
    await t.crawler.crawl_subcategory(t.cat_id)
    assert opened(t) == []


# --- карточки ---


async def test_cap_12_opened_pages_with_fixture(tmp_path):
    html = (FIXTURES / "market_search_s104.html").read_text(encoding="utf-8")
    pages = {f"{CAT_URL}?s=104&pmin=0": html}
    t = setup(tmp_path, pages, min_price=0, report_max_pages=2)
    t.provider.pages = defaultdict(lambda: item_html(1), pages)
    await t.crawler.crawl_subcategory(t.cat_id)
    assert len([u for u in t.provider.calls if "s=104" not in u]) == 12 == t.crawler.settings.report_cards_per_subcat


async def test_second_card_only_if_first_low_vpd(tmp_path):
    group = (("1", "Баян", 20000, "5 дней назад"), ("2", "Баян", 21000, "2 дня назад"))
    # старшая (id 1) первой; высокий vpd -> вторая не открывается
    t = setup(tmp_path, {search_url(1): search_html(*group), card_url(1): item_html(900), card_url(2): item_html(900)})
    await t.crawler.crawl_subcategory(t.cat_id)
    assert opened(t) == [card_url(1)]
    # низкий vpd у первой -> открывается и вторая
    t = setup(
        tmp_path / "2", {search_url(1): search_html(*group), card_url(1): item_html(3), card_url(2): item_html(900)}
    )
    await t.crawler.crawl_subcategory(t.cat_id)
    assert opened(t) == [card_url(1), card_url(2)]
    assert rows(t)[0].external_id == "2" and rows(t)[0].copies == 2


async def test_find_persists_views_page_date_seller_date_through_crawler(tmp_path):
    pages = {
        search_url(1): search_html(("7", "Pioneer XDJ-RX3", 95000, "3 дня назад")),
        card_url(7): item_html(300, THREE_DAYS, today=40),
        SELLER_URL: seller_html("7", "5 часов назад"),
    }
    t = setup(tmp_path, pages)
    assert await t.crawler.crawl_subcategory(t.cat_id)
    (f,) = rows(t)
    assert (f.views, f.today, f.page_date, f.run_id, f.category_id) == (
        300,
        40,
        datetime(2026, 9, 30, 12),
        t.run_id,
        t.cat_id,
    )
    assert f.seller_date == datetime(2026, 10, 3, 7)
    assert f.date_checked and f.age_days == 1.0 and f.vpd == 300 and f.hot  # пересчёт по дате профиля
    assert (f.group_key, f.price_min, f.price_max, f.copies) == ("pioneer xdj rx3", 95000, 95000, 1)
    c = cat_row(t)
    assert (c.last_status, c.last_crawled_at, c.last_run_id, c.last_best_vpd) == ("ok", NOW, t.run_id, 100 * 3)
    with t.sf() as db:
        run = db.get(CrawlRun, t.run_id)
        assert run.loads == t.crawler.loads == 4 and run.finds_count == 1


async def _one_card(tmp_path, item, seller_page=None, **settings):
    pages = {search_url(1): search_html(("7", "Баян", 30000, "3 дня назад")), card_url(7): item}
    if seller_page is not None:
        pages[SELLER_URL] = seller_page
    t = setup(tmp_path, pages, **settings)
    await t.crawler.crawl_subcategory(t.cat_id)
    return t


async def test_seller_checked_recompute_vpd(tmp_path):
    t = await _one_card(tmp_path, item_html(150), seller_html("7", "2 дня назад"))  # 150/3=50 -> 150/2=75
    (f,) = rows(t)
    assert f.date_checked and f.vpd == 75 and not f.hot


async def test_seller_date_old_drops_find(tmp_path):
    t = await _one_card(tmp_path, item_html(900), seller_html("7", "23 июня 12:08"))
    assert rows(t) == []


async def test_seller_recompute_can_drop_below_vpd_min(tmp_path):
    t = await _one_card(tmp_path, item_html(150), seller_html("7", "6 дней назад"))  # 150/6 = 25 < 50
    assert rows(t) == []


async def test_seller_not_found_unchecked(tmp_path):
    t = await _one_card(tmp_path, item_html(900), seller_html("999", "1 день назад"))
    (f,) = rows(t)
    assert not f.date_checked and f.seller_date is None and f.age_days == 3.0
    t = await _one_card(tmp_path / "e", item_html(900), RuntimeError("boom"))  # ошибка профиля — тоже «не проверена»
    assert not rows(t)[0].date_checked


async def test_stale_card_skips_profile(tmp_path):
    t = await _one_card(tmp_path, item_html(900, "20 сентября в 12:00"), seller_html("7", "1 день назад"))
    assert rows(t) == [] and SELLER_URL not in t.provider.calls  # AC-3.5


async def test_seller_check_disabled_marks_date_checked(tmp_path):
    t = await _one_card(tmp_path, item_html(900), check_seller_date=False)
    (f,) = rows(t)
    assert f.date_checked and f.seller_date is None and SELLER_URL not in t.provider.calls


async def test_item_without_views_counter_skipped(tmp_path):
    t = await _one_card(tmp_path, "<html></html>")
    assert rows(t) == [] and cat_row(t).last_status == "ok"


# --- ошибки, таймаут, остановка ---


async def test_subcat_error_skipped_in_summary(tmp_path):
    t = setup(tmp_path, {search_url(1): RuntimeError("упала выдача")})
    assert await t.crawler.crawl_subcategory(t.cat_id) is False
    c = cat_row(t)
    assert c.last_status == "error" and c.last_crawled_at is None and c.last_run_id == t.run_id  # M4
    assert len(t.crawler.errors) == 1 and "Аккордеоны" in t.crawler.errors[0]


async def test_card_error_skipped_run_continues(tmp_path):
    pages = {
        search_url(1): search_html(("1", "Сломанный", 20000, "3 дня назад"), ("2", "Нормальный", 30000, "3 дня назад")),
        card_url(1): RuntimeError("карточка упала"),
        card_url(2): item_html(900),
        SELLER_URL: seller_html("2", "1 день назад"),
    }
    t = setup(tmp_path, pages)
    assert await t.crawler.crawl_subcategory(t.cat_id) is True
    assert [f.external_id for f in rows(t)] == ["2"] and len(t.crawler.errors) == 1


async def test_fetch_timeout_counted_as_error(tmp_path):
    async def hang():
        await asyncio.sleep(10)

    pages = {search_url(1): search_html(("1", "Баян", 20000, "3 дня назад"))}
    t = setup(tmp_path, pages)
    t.crawler.fetch_timeout = 0.01

    class Slow(FakeProvider):
        async def fetch(self, url, ready_selector=None):
            if "/t_" in url:
                await hang()
            return await super().fetch(url, ready_selector)

    t.crawler.provider = Slow(pages)
    assert await t.crawler.crawl_subcategory(t.cat_id) is True
    assert rows(t) == [] and len(t.crawler.errors) == 1 and "TimeoutError" in t.crawler.errors[0]


async def test_interrupted_subcat_no_duplicates_and_loads_kept(tmp_path):
    pages = {
        search_url(1): search_html(("1", "Баян А", 20000, "3 дня назад"), ("2", "Баян Б", 30000, "3 дня назад")),
        card_url(1): item_html(900),
        SELLER_URL: seller_html("1", "1 день назад"),
    }
    t = setup(tmp_path, pages)
    pages[card_url(2)] = lambda: setattr(t.crawler, "stop_requested", True) or item_html(900)
    with pytest.raises(StopRequested):  # /stop приходит во время открытия карточек
        await t.crawler.crawl_subcategory(t.cat_id)
    assert rows(t) == [] and cat_row(t).last_run_id is None
    with t.sf() as db:
        assert db.get(CrawlRun, t.run_id).loads == t.crawler.loads > 0
    t.crawler.stop_requested = False
    pages[card_url(2)] = item_html(900)
    pages[SELLER_URL] = lambda: seller_html("1", "1 день назад")
    await t.crawler.crawl_subcategory(t.cat_id)
    assert len(rows(t)) == 2  # ровно по одной находке на группу, дублей нет


async def test_budget_and_block_propagate_and_persist_loads(tmp_path):
    pages = {
        search_url(1): search_html(("1", "Баян", 20000, "3 дня назад")),
        search_url(2): "<html></html>",
        card_url(1): item_html(900),
    }
    t = setup(tmp_path, pages, report_budget=2)
    with pytest.raises(BudgetExhausted):
        await t.crawler.crawl_subcategory(t.cat_id)
    with t.sf() as db:
        assert db.get(CrawlRun, t.run_id).loads == 2

    t = setup(tmp_path / "b", {search_url(1): ProviderBlocked("капча")})
    with pytest.raises(ProviderBlocked):
        await t.crawler.crawl_subcategory(t.cat_id)
    assert t.crawler.errors == [] and cat_row(t).last_status is None
    with t.sf() as db:
        assert db.get(CrawlRun, t.run_id).loads == 1


# --- разделы (I9b) ---

SECTION = "muzykalnye_instrumenty"
SECTION_URL = f"https://www.avito.ru/rossiya/{SECTION}"
SECTION_HTML = (FIXTURES / "market_section.html").read_text(encoding="utf-8")


def discovery(tmp_path, **settings):
    return setup(tmp_path, {SECTION_URL: SECTION_HTML}, seed=False, report_sections=SECTION, **settings)


async def test_discover_upsert_no_duplicates_on_rerun(tmp_path):
    t = discovery(tmp_path)
    await t.crawler.discover_sections()
    first = {c.url: c.id for c in rows(t, Category)}
    assert len(first) == 8 and all(c.section == SECTION and c.last_crawled_at is None for c in rows(t, Category))
    # раздел устарел -> перечитывается, дублей нет, id и история обхода сохраняются
    with t.sf() as db:
        for c in db.scalars(select(Category)):
            c.discovered_at = datetime(2026, 8, 1)
            c.last_best_vpd = 77
        db.commit()
    await t.crawler.discover_sections()
    again = rows(t, Category)
    assert {c.url: c.id for c in again} == first and all(
        c.discovered_at == NOW and c.last_best_vpd == 77 for c in again
    )


async def test_refresh_30d_fresh_not_reloaded_stale_reloaded(tmp_path):
    t = discovery(tmp_path)
    await t.crawler.discover_sections()
    await t.crawler.discover_sections()  # свежие (только что найдены) — не грузим
    assert t.provider.calls == [SECTION_URL]
    with t.sf() as db:
        for c in db.scalars(select(Category)):
            c.discovered_at = NOW - timedelta(days=31)
        db.commit()
    await t.crawler.discover_sections()
    assert t.provider.calls == [SECTION_URL, SECTION_URL]


async def test_max_subcats_and_loads_counted(tmp_path):
    t = discovery(tmp_path, report_max_subcats=3)
    await t.crawler.discover_sections()
    assert len(rows(t, Category)) == 3 and t.crawler.loads == 1
    with t.sf() as db:
        assert db.get(CrawlRun, t.run_id).loads == 1


async def test_discover_error_skipped_other_sections_continue(tmp_path):
    t = setup(
        tmp_path,
        {SECTION_URL: SECTION_HTML, "https://www.avito.ru/rossiya/telefony": RuntimeError("x")},
        seed=False,
        report_sections=f"telefony,{SECTION}",
    )
    await t.crawler.discover_sections()
    assert len(rows(t, Category)) == 8 and len(t.crawler.errors) == 1
