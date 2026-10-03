import asyncio
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db import Category, CrawlRun, Find, init_db
from app.providers.base import AvitoProvider, BrowserGate, Page, ProviderBlocked
from app.services.market import BudgetExhausted, MarketCrawler, StopRequested

NOW = datetime(2026, 10, 3, 12, 0)
CAT_URL = "https://www.avito.ru/all/muzykalnye_instrumenty/akkordeony-ASgB"
FIXTURES = Path(__file__).parent / "fixtures"
THREE_DAYS = "30 сентября в 12:00"  # возраст 3 дня
# без пауз после блока и без общего темпа Avito: их проверяют test_cooldown / test_traffic
FAST = {"block_cooldowns": 0, "avito_min_interval_s": 0, "avito_max_per_hour": 10**6, "avito_daily_budget": 10**6,
        "max_blocks_per_day": 10**6, "post_captcha_cooldown_min": 0}  # fmt: skip


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
        self.events: list = []  # общий журнал с уведомителем
        self.enters = self.exits = 0
        self.human = False  # прошёл ли человек капчу
        self.waits: list = []

    async def __aenter__(self):
        self.enters += 1
        return self

    async def __aexit__(self, *exc):
        self.exits += 1

    async def wait_unblocked(self, url, timeout_s, poll_s=5, cancel=None):
        self.waits.append((url, timeout_s))
        return self.human

    async def search(self, search, page=1):
        raise AssertionError

    async def fetch(self, url, ready_selector=None):
        await asyncio.sleep(0)  # как настоящий ввод-вывод: другие задачи успевают вклиниться
        self.calls.append(url)
        self.events.append(("fetch", url))
        res = "<html></html>" if "s=104" in url and url not in self.pages else self.pages[url]  # KeyError = ошибка
        if isinstance(res, BaseException):
            raise res
        if callable(res):
            res = res()
        return Page(res, "", url)


class FakeNotifier:
    async def send_text(self, text, markup=None):
        return 1

    async def edit_text(self, message_id, text):
        pass


def setup(tmp_path, pages: dict, seed: bool = True, **settings):
    sf = init_db(f"sqlite:///{tmp_path}/t.db")
    settings = FAST | settings
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
    assert await t.crawler.crawl_subcategory(t.cat_id) is False  # все открытые карточки упали = ошибка (ADR-007)
    assert rows(t) == [] and len(t.crawler.errors) == 2 and "TimeoutError" in t.crawler.errors[0]


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


# --- жизненный цикл (I10) ---


class RecNotifier:
    def __init__(self, events):
        self.events, self.sent, self.edits, self.fail = events, [], [], False
        self.markups: list = []

    async def send_text(self, text, markup=None):
        self.markups.append(markup)
        self.events.append(("send", text))
        if self.fail:
            return None
        self.sent.append(text)
        return len(self.sent)

    async def edit_text(self, message_id, text):
        self.events.append(("edit", text))
        self.edits.append((message_id, text))


def life(tmp_path, cats: dict[str, list[int]], gate=None, **settings):
    """cats: раздел -> просмотры карточки по подкатегориям (у каждой одна карточка с находкой)."""
    sf = init_db(f"sqlite:///{tmp_path}/t.db")
    settings = FAST | settings
    s = Settings(_env_file=None, report_sections=",".join(cats), check_seller_date=False, **settings)
    pages: dict = {}
    with sf() as db:
        for sec, views in cats.items():
            for n, v in enumerate(views, 1):
                url = f"https://www.avito.ru/all/{sec}/c{n}-H"
                db.add(Category(section=sec, name=f"{sec} {n}", url=url, discovered_at=NOW))
                cid = f"{sec}{n}"
                pages[f"{url}?s=104&pmin=10000"] = search_html((cid, f"Item {sec} {n}", 20000, "1 день назад"))
                pages[card_url(cid)] = item_html(v)
        db.commit()
    provider = FakeProvider(pages)
    t = SimpleNamespace(sf=sf, provider=provider, pages=pages, now=[NOW], settings=s)
    t.notifier = RecNotifier(provider.events)
    t.crawler = MarketCrawler(sf, lambda: provider, t.notifier, s, gate=gate, clock=lambda: t.now[0])
    t.crawler.send_delay = 0
    return t


def runs(t) -> list[CrawlRun]:
    return rows(t, CrawlRun)


def summary_text(t) -> str:
    """Итог (формат D) может состоять из нескольких сообщений: от шапки до конца."""
    sent = t.notifier.sent
    start = max(i for i, x in enumerate(sent) if "Проверка рынка ·" in x)
    return "\n\n".join(sent[start:])


def search_calls(t, sec_n: str) -> int:
    return sum(1 for u in t.provider.calls if f"/{sec_n}-H?s=104" in u)


def url_of(sec_n: str) -> str:
    return f"https://www.avito.ru/all/{sec_n[0]}/{sec_n[1:]}-H?s=104&pmin=10000"


def hook(t, url: str, fn) -> None:
    """При загрузке url выполнить fn() и отдать прежний HTML."""
    html = t.pages[url]
    t.pages[url] = lambda: fn() or html


async def test_start_new(tmp_path):
    t = life(tmp_path, {"A": [900]})
    assert t.crawler.start() == "Начинаю проверку рынка"
    await t.crawler._task
    (run,) = runs(t)
    assert run.status == "done" and run.finished_at and run.finds_count == 1
    assert run.loads == t.crawler.loads == 4  # warm-up + выдача (2 стр.) + карточка
    assert t.notifier.sent[0].startswith("<b>🔎 Проверка рынка") and "📊 Проверка рынка" in summary_text(t)
    assert not t.crawler.running


async def test_already_running_shows_progress(tmp_path):
    t = life(tmp_path, {"A": [900]})
    t.crawler.start()
    again = t.crawler.start()
    assert again.startswith("Проверка уже идёт\n<b>🔎 Проверка рынка</b>\n\n<code>▱")
    await t.crawler._task
    assert len(runs(t)) == 1


async def test_budget_stop_remaining_msg_and_loads_persist(tmp_path):
    t = life(tmp_path, {"A": [900, 900, 900]}, report_budget=6)
    t.crawler.start()
    await t.crawler._task
    (run,) = runs(t)
    assert run.status == "budget" and run.loads == 6  # частичная подкатегория тоже учтена
    text = summary_text(t)
    assert "Бюджет загрузок исчерпан." in text and "Осталось 2 подкатегорий, пойдут первыми" in text


async def _stopped_run(tmp_path):
    t = life(tmp_path, {"A": [900, 900]})
    hook(t, url_of("Ac2"), lambda: setattr(t.crawler, "stop_requested", True))  # /stop на второй подкатегории
    t.crawler.start()
    await t.crawler._task
    t.pages[url_of("Ac2")] = search_html(("A2", "Item A 2", 20000, "1 день назад"))
    return t


async def test_stop_summary(tmp_path):
    t = await _stopped_run(tmp_path)
    (run,) = runs(t)
    assert run.status == "stopped" and run.loads == 5  # warm-up, 3 загрузки c1, 1 загрузка c2 (прервана)
    assert "остановлена" in summary_text(t) and "Item A 1" in summary_text(t)
    with t.sf() as db:
        assert db.scalars(select(Category).where(Category.name == "A 2")).one().last_run_id is None


async def test_resume_lt_12h_no_repeats(tmp_path):
    t = await _stopped_run(tmp_path)
    t.now[0] += timedelta(hours=11)
    assert t.crawler.start() == "Продолжаю проверку (пройдено подкатегорий: 1)"
    await t.crawler._task
    (run,) = runs(t)
    assert run.status == "done" and run.finds_count == 2 and run.loads > 5
    assert search_calls(t, "c1") == 2  # две страницы выдачи в первом заходе, при продолжении не открывалась
    assert sum(1 for x in t.notifier.sent if x.startswith("<b>🔎")) == 2  # на продолжении новое сообщение о прогрессе


async def test_new_run_gt_12h(tmp_path):
    t = await _stopped_run(tmp_path)
    t.now[0] += timedelta(hours=13)
    assert t.crawler.start() == "Начинаю проверку рынка"
    await t.crawler._task
    assert [r.status for r in runs(t)] == ["stopped", "done"]


async def test_progress_once_per_minute(tmp_path):
    t = life(tmp_path, {"A": [900, 900, 900]})
    hook(t, url_of("Ac2"), lambda: t.now.__setitem__(0, NOW + timedelta(seconds=61)))
    t.crawler.start()
    await t.crawler._task
    assert sum(1 for x in t.notifier.sent if x.startswith("<b>🔎")) == 1
    assert len(t.notifier.edits) == 2 and t.notifier.edits[0][1].startswith("<b>🔎")  # до c2 тихо, после c2 одна правка
    last = t.notifier.edits[-1][1]
    assert last.startswith("<b>✅ Проверка завершена</b>") and "Прошло: <code>01:01</code>" in last


async def test_hot_sent_immediately_non_hot_only_in_summary(tmp_path):
    t = life(tmp_path, {"A": [900, 150], "B": [450]})  # vpd 300 🔥, 50, 150 🔥
    t.crawler.start()
    await t.crawler._task
    ev = t.provider.events
    sends = [e[1] for e in ev if e[0] == "send"]
    cards = [x for x in sends if "💡" in x]
    assert len(cards) == 2 and "Item A 1" in cards[0] and "Item B 1" in cards[1]  # только 🔥, формат карточки
    assert not any("— находки" in x for x in sends)  # порций по разделам больше нет
    card_a = next(i for i, e in enumerate(ev) if e[0] == "send" and "💡" in e[1] and "Item A 1" in e[1])
    next_fetch = next(i for i, e in enumerate(ev) if e[0] == "fetch" and "/A/c2-H" in e[1])
    assert card_a < next_fetch  # 🔥 ушла сразу после подкатегории, до следующей
    buttons = [m for m in t.notifier.markups if m is not None]
    assert len(buttons) == 2 and all(len(m.inline_keyboard) == 1 for m in buttons)
    with t.sf() as db:
        ids = {f.title: f.id for f in db.scalars(select(Find))}
    a1 = ids["Item A 1"]
    assert [b.callback_data for b in buttons[0].inline_keyboard[0]] == [f"fb:{a1}:1", f"fb:{a1}:-1"]
    summary = summary_text(t)
    assert all(f"Item {x}" in summary for x in ("A 1", "A 2", "B 1"))  # в итоге все находки, 🔥 тоже
    lines = {
        x: next(ln for ln in summary.split("\n") if f"Item {x}" in ln).replace("<blockquote expandable>", "")
        for x in ("A 1", "A 2", "B 1")
    }
    assert lines["A 1"].startswith("🔥 ") and lines["B 1"].startswith("🔥 ") and not lines["A 2"].startswith("🔥")
    assert "<b>📦 A — 2</b>" in summary and "<b>📦 B — 1</b>" in summary
    assert summary.index("Item A 1") < summary.index("Item A 2")  # внутри раздела по vpd
    assert "Найдено: <code>3</code> · 🔥 <code>2</code>" in summary and "Подкатегорий: <code>3</code>" in summary
    with t.sf() as db:
        sent = {f.title: f.sent for f in db.scalars(select(Find))}
    assert sent == {"Item A 1": True, "Item A 2": False, "Item B 1": True}


async def test_failed_hot_send_stays_unsent_but_in_summary(tmp_path):
    t = life(tmp_path, {"A": [900]})
    t.notifier.fail = True
    t.crawler.start()
    await t.crawler._task
    assert runs(t)[0].status == "done" and not rows(t)[0].sent  # карточка не ушла: sent=False
    summary = [e[1] for e in t.provider.events if e[0] == "send" and "Проверка рынка ·" in e[1]]
    assert summary and "Item A 1" in summary[0]  # но в итоге она есть


async def test_already_seen_from_db_marks_it_in_summary(tmp_path):
    t = life(tmp_path, {"A": [900, 450]})  # c1: vpd 300, c2: vpd 150
    with t.sf() as db:
        old = CrawlRun(started_at=datetime(2026, 9, 20), status="done")
        db.add(old)
        db.flush()
        cid = db.scalars(select(Category).where(Category.name == "A 1")).one().id
        db.add(
            Find(
                run_id=old.id, category_id=cid, group_key="item a 1", title="x", price_min=1, price_max=1, vpd=200,
                age_days=2, date_checked=True, copies=1, url="u", external_id="9", hot=True,
                created_at=datetime(2026, 9, 20, 10), sent=True,
            )
        )  # fmt: skip
        db.commit()
    t.crawler.start()
    await t.crawler._task
    lines = summary_text(t).split("\n")
    seen, new = (next(ln for ln in lines if f"Item A {n}" in ln) for n in (1, 2))
    assert "уже было" in seen and "уже было" not in new
    card = next(x for x in t.notifier.sent if "💡" in x and "Item A 1" in x)
    assert card.count("уже было 20.09") == 1  # и в карточке с датой


def test_mark_interrupted(tmp_path):
    t = life(tmp_path, {"A": [900]})
    with t.sf() as db:
        db.add_all([CrawlRun(started_at=NOW, status="running"), CrawlRun(started_at=NOW, status="done")])
        db.commit()
    t.crawler.mark_interrupted()
    assert [r.status for r in runs(t)] == ["interrupted", "done"]


async def test_stop_command_replies(tmp_path):
    t = life(tmp_path, {"A": [900]})
    assert t.crawler.stop() == "Проверка не идёт"
    t.crawler.start()
    assert t.crawler.stop().startswith("Останавливаю")
    await t.crawler._task
    assert runs(t)[0].status == "stopped"


async def test_crash_marks_failed_and_releases_gate(tmp_path):
    gate = BrowserGate()
    t = life(tmp_path, {"A": [900]}, gate=gate)

    def boom():
        raise RuntimeError("браузер не стартовал")

    t.crawler.provider_factory = boom
    t.crawler.start()
    await t.crawler._task
    (run,) = runs(t)
    assert run.status == "failed" and "упала" in summary_text(t) and not gate.locked


async def test_gate_held_during_run_and_released(tmp_path):
    gate = BrowserGate()
    t = life(tmp_path, {"A": [900]}, gate=gate)
    seen = []
    hook(t, url_of("Ac1"), lambda: seen.append(gate.locked))
    t.crawler.start()
    await t.crawler._task
    assert seen == [True] and not gate.locked


# --- уступка браузера, блок, предохранитель (I11) ---


def start_monitor(t, gate, tasks):
    """Мониторинг D&G: берёт замок, как Scanner.run_watch_rules."""

    async def monitor():
        async with gate.hold():
            t.provider.events.append(("monitor", None))

    return lambda: tasks.append(asyncio.create_task(monitor()))


def fetches_between_hook_and_monitor(t, hook_url):
    ev = t.provider.events
    i = next(i for i, e in enumerate(ev) if e == ("fetch", hook_url))
    j = next(i for i, e in enumerate(ev) if e[0] == "monitor")
    return sum(1 for e in ev[i:j] if e[0] == "fetch") - 1


async def test_scan_gets_gate_within_one_load_and_handover_loses_nothing(tmp_path):
    gate, tasks = BrowserGate(), []
    t = life(tmp_path, {"A": [900, 900]}, gate=gate)
    t.crawler.reopen_delay = 0
    hook(t, url_of("Ac1"), start_monitor(t, gate, tasks))  # проверка D&G встаёт в очередь во время обхода c1
    t.crawler.start()
    await t.crawler._task
    await asyncio.gather(*tasks)
    assert (
        fetches_between_hook_and_monitor(t, url_of("Ac1")) <= 2
    )  # уступили в пределах одной загрузки, не подкатегории
    assert t.provider.enters == t.provider.exits == 2  # старый браузер закрыт, новый открыт
    (run,) = runs(t)
    assert run.status == "done" and run.finds_count == 2  # посреди подкатегории ничего не потеряно
    assert run.loads == 8  # warm-up + 6 загрузок + warm-up нового браузера
    assert not gate.locked


async def test_reopen_error_after_release_no_runtime_error_and_cause_kept(tmp_path):
    gate, tasks = BrowserGate(), []
    t = life(tmp_path, {"A": [900] * 4}, gate=gate)
    t.crawler.reopen_delay = 0
    opened = []

    def factory():
        opened.append(1)
        if len(opened) > 1:
            raise RuntimeError("профиль занят")
        return t.provider

    t.crawler.provider_factory = factory
    hook(t, url_of("Ac1"), start_monitor(t, gate, tasks))
    t.crawler.start()
    await t.crawler._task
    await asyncio.gather(*tasks)
    assert runs(t)[0].status == "failed" and not gate.locked
    assert "профиль занят" in t.crawler.errors[0] and not any("not acquired" in e for e in t.crawler.errors)


async def test_reopen_retry_after_profile_in_use(tmp_path):
    gate, tasks = BrowserGate(), []
    t = life(tmp_path, {"A": [900, 900]}, gate=gate)
    t.crawler.reopen_delay = 0
    calls = []

    def factory():
        calls.append(1)
        if len(calls) == 2:  # первая попытка переоткрытия: профиль ещё занят
            raise RuntimeError("profile in use")
        return t.provider

    t.crawler.provider_factory = factory
    hook(t, url_of("Ac1"), start_monitor(t, gate, tasks))
    t.crawler.start()
    await t.crawler._task
    assert runs(t)[0].status == "done" and runs(t)[0].finds_count == 2 and len(calls) == 3


def break_page(t, sec_n, times=None):
    """Страница выдачи отдаёт блок (всегда или times раз)."""
    url, html, n = url_of(sec_n), t.pages[url_of(sec_n)], [0]

    def page():
        n[0] += 1
        if times is None or n[0] <= times:
            raise ProviderBlocked("Доступ ограничен (HTTP 439)")
        return html

    t.pages[url] = page
    return html


async def test_block_midrun_resumable(tmp_path):
    t = life(tmp_path, {"A": [900, 900]})
    html = break_page(t, "Ac2")
    t.crawler.start()
    await t.crawler._task
    (run,) = runs(t)
    assert run.status == "blocked" and "Item A 1" in summary_text(t)  # найденное сохранено и отправлено
    assert any("Avito просит проверку" in x and "<code>15</code> мин" in x for x in t.notifier.sent)  # ADR-005
    assert len(t.provider.waits) == 1 and t.provider.waits[0][1] == 15 * 60 and "ограничил" in summary_text(t)
    t.pages[url_of("Ac2")] = html
    t.now[0] += timedelta(hours=1)
    assert t.crawler.start() == "Продолжаю проверку (пройдено подкатегорий: 1)"
    await t.crawler._task
    assert runs(t)[0].status == "done" and runs(t)[0].finds_count == 2 and search_calls(t, "c1") == 2


async def test_block_human_passes_crawl_continues(tmp_path):
    t = life(tmp_path, {"A": [900, 900]})
    t.provider.human = True
    break_page(t, "Ac2", times=1)
    t.crawler.start()
    await t.crawler._task
    assert runs(t)[0].status == "done" and runs(t)[0].finds_count == 2
    assert any("🧩" in x for x in t.notifier.sent) and any("✅ Проверка пройдена" in x for x in t.notifier.sent)


async def test_block_headless_no_wait(tmp_path):
    t = life(tmp_path, {"A": [900, 900]}, headless=True)
    break_page(t, "Ac2")
    t.crawler.start()
    await t.crawler._task
    assert runs(t)[0].status == "blocked" and t.provider.waits == []
    assert not any("🧩" in x for x in t.notifier.sent)


async def test_breaker_three_errors_failed_one_summary(tmp_path):
    t = life(tmp_path, {"A": [900] * 4})
    for n in (1, 2, 3):
        t.pages[url_of(f"Ac{n}")] = RuntimeError("Target closed")
    t.crawler.start()
    await t.crawler._task
    assert runs(t)[0].status == "failed" and len(t.crawler.errors) == 3
    assert search_calls(t, "c4") == 0
    assert sum("📊 Проверка рынка" in x for x in t.notifier.sent) == 1 and "упала" in summary_text(t)
    with t.sf() as db:
        cats = db.scalars(select(Category).order_by(Category.id)).all()
        assert [c.last_status for c in cats] == ["error"] * 3 + [None] and all(c.last_crawled_at is None for c in cats)


async def test_crawler_crash_monitor_unaffected(tmp_path):
    gate = BrowserGate()
    t = life(tmp_path, {"A": [900]}, gate=gate)

    async def boom(text):
        raise RuntimeError("telegram упал")

    t.notifier.send_text = boom
    t.crawler.start()
    await t.crawler._task  # задача завершилась без исключения
    assert runs(t)[0].status == "failed" and not gate.locked
    async with gate.hold():  # мониторинг получает браузер как обычно
        pass


async def test_progress_final_headers_and_current_states(tmp_path):
    t = life(tmp_path, {"A": [900, 900]})
    seen: list[str] = []
    hook(t, url_of("Ac2"), lambda: seen.append(t.crawler._progress_text()))
    t.crawler.start()
    await t.crawler._task
    assert "📦 A — A 2" in seen[0]  # во время обхода подкатегории
    assert t.notifier.edits[-1][1].split("\n")[0] == "<b>✅ Проверка завершена</b>"
    t = await _stopped_run(tmp_path / "s")
    assert t.notifier.edits[-1][1].startswith("<b>⏹ Остановлено</b>")
    t = life(tmp_path / "b", {"A": [900, 900]})
    break_page(t, "Ac2")
    t.crawler.start()
    await t.crawler._task
    assert t.notifier.edits[-1][1].startswith("<b>⚠️ Блок Avito</b>")


async def test_progress_captcha_state_and_recheck_state(tmp_path):
    t = life(tmp_path, {"A": [900]})
    states = []

    async def wait(url, timeout_s, poll_s=5, cancel=None):
        states.append(t.crawler._progress_text())
        return True

    t.provider.wait_unblocked = wait
    break_page(t, "Ac1", times=1)
    t.crawler.start()
    await t.crawler._task
    assert "🧩 жду проверку капчи" in states[0] and "🧩 жду" not in t.crawler._progress_text()
    assert any("🧩 жду проверку капчи" in text for _, text in t.notifier.edits)  # сообщение правится сразу
    t = life(tmp_path / "r", {"A": [900]})
    with t.sf() as db:
        db.add(
            Find(run_id=1, category_id=1, group_key="g", title="Old", price_min=1, price_max=1, vpd=1, age_days=1,
                 date_checked=True, copies=1, url=card_url("o"), external_id="o", hot=False,
                 created_at=NOW - timedelta(days=3))
        )  # fmt: skip
        db.commit()
    t.pages[card_url("o")] = lambda: seen_text.append(t.crawler._progress_text()) or item_html(5)
    seen_text: list[str] = []
    t.crawler.start()
    await t.crawler._task
    assert "перепроверка находок" in seen_text[0]


async def test_progress_ticker_edits_during_long_subcategory_and_skips_unchanged(tmp_path):
    t = life(tmp_path, {"A": [900]})
    t.crawler.settings.progress_edit_seconds = 0.02

    class Slow(FakeProvider):
        async def fetch(self, url, ready_selector=None):
            t.now[0] += timedelta(seconds=5)  # часы бегут, пока «грузится» страница
            await asyncio.sleep(0.1)
            return await super().fetch(url, ready_selector)

    slow = Slow(t.pages)
    slow.events = t.provider.events
    t.crawler.provider_factory = lambda: slow
    t.crawler.start()
    await t.crawler._task
    live = [x for _, x in t.notifier.edits if x.startswith("<b>🔎")]
    assert len(live) >= 3 and len(set(live)) == len(live)  # правки по таймеру, одинаковый текст не шлётся
    assert t.crawler._ticker is None


async def test_query_category_url_keeps_q_and_adds_sort(tmp_path):
    t = setup(tmp_path, {})
    with t.sf() as db:
        db.get(Category, t.cat_id).url = f"{CAT_URL}?q=prada"
        db.commit()
    await t.crawler.crawl_subcategory(t.cat_id)
    assert t.provider.calls[0] == f"{CAT_URL}?q=prada&s=104&pmin=10000"
