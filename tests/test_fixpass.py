"""Тесты fix-pass после финального ревью (ADR-007)."""

import asyncio
import html as htmllib
import time
from datetime import datetime, timedelta
from types import SimpleNamespace

from app.providers import avito_browser
from app.providers.avito_browser import AvitoBrowserProvider
from app.providers.avito_parser import (
    is_avito_url,
    is_blocked,
    parse_item_page,
    parse_published,
    parse_search_html,
    parse_subcategories,
)
from app.providers.base import ProviderBlocked
from app.services.market_logic import (
    SECTION_NAMES,
    group_cards,
    pick_groups,
    section_name,
    split_message,
)
from tests.test_market import (
    NOW,
    SELLER_URL,
    card_url,
    cat_row,
    item_html,
    life,
    opened,
    rows,
    runs,
    search_html,
    search_url,
    seller_html,
    setup,
    summary_text,
    url_of,
)
from tests.test_market_logic import card

# --- HIGH: копии и цены по полной группе ---

THREE = (("1", "Баян", 20000, "5 дней назад"), ("2", "Баян", 21000, "4 дня назад"), ("3", "Баян", 22000, "3 дня назад"))


async def test_copies_and_price_range_from_full_group(tmp_path):
    pages = {
        search_url(1): search_html(*THREE),
        card_url(1): item_html(900),
        SELLER_URL: seller_html("1", "3 дня назад"),
    }
    t = setup(tmp_path, pages)
    await t.crawler.crawl_subcategory(t.cat_id)
    (f,) = rows(t)
    assert opened(t) == [card_url(1)]  # открыта одна, но группа из трёх
    assert (f.copies, f.price_min, f.price_max) == (3, 20000, 22000)


async def test_third_copy_unopened_when_two_opened_are_low(tmp_path):
    pages = {search_url(1): search_html(*THREE), card_url(1): item_html(3), card_url(2): item_html(3)}
    t = setup(tmp_path, pages)
    assert await t.crawler.crawl_subcategory(t.cat_id) is True
    assert opened(t) == [card_url(1), card_url(2)] and rows(t) == []  # AC-2.4a: третья не открывается


async def test_second_copy_find_still_counts_all_three(tmp_path):
    pages = {
        search_url(1): search_html(*THREE),
        card_url(1): item_html(3),
        card_url(2): item_html(900),
        SELLER_URL: seller_html("2", "4 дня назад"),
    }
    t = setup(tmp_path, pages)
    await t.crawler.crawl_subcategory(t.cat_id)
    (f,) = rows(t)
    assert f.external_id == "2" and f.copies == 3 and (f.price_min, f.price_max) == (20000, 22000)


def test_pick_groups_returns_full_group():
    g = [card("1", "Баян", 100, 5), card("2", "Баян", 101, 4), card("3", "Баян", 102, 3)]
    ((picked, full),) = pick_groups(group_cards(g))
    assert [c.external_id for c in picked] == ["1", "2"] and len(full) == 3


# --- BrowserLost, все карточки упали ---


async def test_browser_lost_midway_is_error_not_crawled(tmp_path):
    pages = {search_url(1): search_html(("1", "Баян А", 20000, "3 дня назад")), card_url(1): item_html(900)}
    t = setup(tmp_path, pages)
    pages[search_url(1)] = lambda: (
        setattr(t.crawler, "provider", None) or search_html(("1", "Баян А", 20000, "3 дня назад"))
    )
    assert await t.crawler.crawl_subcategory(t.cat_id) is False
    c = cat_row(t)
    assert c.last_status == "error" and c.last_crawled_at is None
    assert any("BrowserLost" in e for e in t.crawler.errors)


async def test_all_cards_failed_counts_as_error(tmp_path):
    pages = {
        search_url(1): search_html(("1", "Баян А", 20000, "3 дня назад"), ("2", "Гитара", 30000, "3 дня назад")),
        card_url(1): RuntimeError("boom"),
        card_url(2): RuntimeError("boom"),
    }
    t = setup(tmp_path, pages)
    assert await t.crawler.crawl_subcategory(t.cat_id) is False
    assert cat_row(t).last_status == "error"


async def test_browser_lost_trips_breaker(tmp_path):
    t = life(tmp_path, {"A": [900] * 5})
    t.crawler.provider_factory = lambda: t.provider
    t.crawler.reopen_delay = 0

    def lose():
        t.crawler.provider = None  # браузер пропал посреди прогона

    hook = t.pages[url_of("Ac1")]
    t.pages[url_of("Ac1")] = lambda: lose() or hook
    t.crawler.start()
    await t.crawler._task
    assert runs(t)[0].status == "failed" and search_calls_total(t) <= 2


def search_calls_total(t) -> int:
    return sum(1 for u in t.provider.calls if "s=104" in u)


# --- Resume: свежий бюджет, накопительные loads ---


async def test_resume_gets_fresh_budget_and_cumulative_loads(tmp_path):
    t = life(tmp_path, {"A": [900, 900]}, report_budget=8)
    from tests.test_market import hook

    hook(t, url_of("Ac2"), lambda: setattr(t.crawler, "stop_requested", True))
    t.crawler.start()
    await t.crawler._task
    assert runs(t)[0].status == "stopped" and runs(t)[0].loads == 5
    t.pages[url_of("Ac2")] = search_html(("A2", "Item A 2", 20000, "1 день назад"))
    t.now[0] += timedelta(hours=1)
    assert t.crawler.start().startswith("Продолжаю")
    assert t.crawler.loads == 0  # бюджет заново
    await t.crawler._task
    (run,) = runs(t)
    assert run.status == "done" and run.loads == 5 + t.crawler.loads and run.loads > 8  # статистика копится


# --- Итог: только неотправленное ---


async def test_summary_only_unsent_finds_plus_totals(tmp_path):
    t = life(tmp_path, {"A": [900], "B": [900, 900]})
    from tests.test_market import hook

    hook(t, url_of("Bc2"), lambda: setattr(t.crawler, "stop_requested", True))
    t.crawler.start()
    await t.crawler._task
    text = summary_text(t)
    assert "Item B 1" in text and "Item A 1" not in text  # A ушла порцией, B 1 — нет
    assert "Всего: найдено 2, 🔥 2, подкатегорий 2, загрузок" in text
    assert sum(1 for x in t.notifier.sent if "Item A 1" in x) == 1


async def test_progress_send_failure_not_retried_every_minute(tmp_path):
    from tests.test_market import hook

    t = life(tmp_path, {"A": [900, 900, 900]})
    t.notifier.fail = True
    hook(t, url_of("Ac2"), lambda: t.now.__setitem__(0, NOW + timedelta(seconds=61)))
    hook(t, url_of("Ac3"), lambda: t.now.__setitem__(0, NOW + timedelta(seconds=130)))
    t.crawler.start()
    await t.crawler._task
    assert sum(1 for e in t.provider.events if e[0] == "send" and e[1].startswith("<b>⏳")) == 1


async def test_finish_db_error_still_sends_summary(tmp_path):
    t = life(tmp_path, {"A": [900]})

    def boom(status):
        raise RuntimeError("database is locked")

    t.crawler._finish = boom
    t.crawler.start()
    await t.crawler._task
    assert "Итог проверки" in summary_text(t)


async def test_portion_header_uses_escaped_section_name(tmp_path):
    t = life(tmp_path, {"A&B": [900]})
    t.crawler.start()
    await t.crawler._task
    assert any("<b>A&amp;B — находки</b>" in x for x in t.notifier.sent)


def test_section_names_cover_all_top_sections():
    from app.config import TOP_SECTIONS, split_csv

    assert set(split_csv(TOP_SECTIONS)) == set(SECTION_NAMES) and len(SECTION_NAMES) == 25
    assert section_name("muzykalnye_instrumenty") == "Музыкальные инструменты"
    assert section_name("new_one") == "new one"


# --- Капча: /stop во время ожидания, CAPTCHA_WAIT_MINUTES=0, повторная загрузка ---


async def test_stop_during_captcha_wait_gives_stopped(tmp_path):
    from tests.test_market import break_page

    t = life(tmp_path, {"A": [900, 900]})

    async def wait(url, timeout_s, poll_s=5, cancel=None):
        t.crawler.stop_requested = True  # /stop пришёл, пока ждали человека
        return not cancel()

    t.provider.wait_unblocked = wait
    break_page(t, "Ac2")
    t.crawler.start()
    await t.crawler._task
    assert runs(t)[0].status == "stopped"


async def test_captcha_wait_zero_minutes_no_wait(tmp_path):
    from tests.test_market import break_page

    t = life(tmp_path, {"A": [900, 900]}, captcha_wait_minutes=0)
    break_page(t, "Ac2")
    t.crawler.start()
    await t.crawler._task
    assert runs(t)[0].status == "blocked" and t.provider.waits == []
    assert not any("🧩" in x for x in t.notifier.sent)


async def test_retry_after_captcha_counts_as_load(tmp_path):
    from tests.test_market import break_page

    clean = life(tmp_path / "c", {"A": [900, 900]})
    clean.crawler.start()
    await clean.crawler._task
    t = life(tmp_path / "b", {"A": [900, 900]})
    t.provider.human = True
    break_page(t, "Ac2", times=1)
    t.crawler.start()
    await t.crawler._task
    assert runs(t)[0].loads == runs(clean)[0].loads + 1  # блок и повтор — две загрузки


# --- wait_unblocked на заглушке вкладки ---


class Tab:
    def __init__(self, blocked_for: int = 0, hang: bool = False):
        self.left, self.hang, self.closed, self.probes = blocked_for, hang, False, 0

    async def goto(self, url, **kw):
        pass

    async def content(self):
        if self.hang:
            await asyncio.sleep(10)
        self.probes += 1
        self.left -= 1
        return "<html>Вы робот?</html>" if self.left >= 0 else "<html>ok</html>"

    async def title(self):
        return ""

    async def close(self):
        self.closed = True


def provider_with(tab):
    p = AvitoBrowserProvider("./x", headless=False)
    p._ctx = SimpleNamespace(new_page=lambda: _coro(tab))
    return p


async def _coro(v):
    return v


async def test_wait_unblocked_passes_when_unblocked():
    tab = Tab(blocked_for=2)
    assert await provider_with(tab).wait_unblocked("u", 5, 0.01) is True
    assert tab.probes == 3 and tab.closed


async def test_wait_unblocked_times_out_by_wall_clock():
    tab = Tab(blocked_for=10**6)
    t0 = time.monotonic()
    assert await provider_with(tab).wait_unblocked("u", 0.1, 0.01) is False
    assert time.monotonic() - t0 < 1 and tab.closed


async def test_wait_unblocked_hanging_content_does_not_extend_wait(monkeypatch):
    monkeypatch.setattr(avito_browser, "PROBE_TIMEOUT", 0.02)
    tab = Tab(hang=True)
    t0 = time.monotonic()
    assert await provider_with(tab).wait_unblocked("u", 0.1, 0.01) is False
    assert time.monotonic() - t0 < 1  # без wait_for ждали бы 10 с


async def test_wait_unblocked_cancel_stops_early():
    tab, flag = Tab(blocked_for=10**6), {"stop": False}
    task = asyncio.create_task(provider_with(tab).wait_unblocked("u", 30, 0.01, lambda: flag["stop"]))
    await asyncio.sleep(0.05)
    flag["stop"] = True
    assert await asyncio.wait_for(task, 1) is False and tab.closed


# --- Детектор блока ---


def test_robot_in_item_description_is_not_block():
    descr = "<div>Вы робот? Нет, продаю баян. Доступ ограничен.</div>"
    page = "<html><body><h1>Баян</h1>" + "<p>текст</p>" * 600 + descr
    assert not is_blocked(page, "Баян купить")
    assert is_blocked("<html><body>Доступ ограничен</body></html>")  # короткая страница блока
    assert is_blocked("<html></html>", "Вы робот?")  # заголовок
    assert is_blocked('<div class="firewall-container">x</div>')  # вёрстка файрвола
    assert not is_blocked("<script>var s='Вы робот'</script><body>ok</body>")


# --- split_message ---


def test_split_message_keeps_order_and_cuts_at_comma():
    long_line = ", ".join(f"Раздел {i} — {i} из 7 дн" for i in range(40))
    assert len(long_line) > 400
    text = "\n".join(["head", long_line, "tail"])
    chunks = split_message(text, 200)
    assert chunks[0].startswith("head") or chunks[0] == "head"
    assert chunks[-1].endswith("tail") and all(len(c) <= 200 for c in chunks)
    joined = "".join(chunks).replace("\n", "")
    assert joined == text.replace("\n", "")  # ничего не потеряно и не перепутано
    assert all(c.rstrip().endswith(",") for c in chunks[1:-1])  # режем по «, »


def test_split_message_does_not_cut_inside_entity_or_tag():
    line = "a" * 95 + "&amp;" + "b" * 50
    chunks = split_message(line, 98)
    assert "".join(chunks) == line and all("&amp;" in c or "&" not in c for c in chunks)
    tagged = "x" * 90 + '<a href="u">t</a>'
    chunks = split_message(tagged, 95)
    assert "".join(chunks) == tagged and chunks[0] == "x" * 90


# --- Безопасность ---


def test_is_avito_url_allowlist():
    assert is_avito_url("https://www.avito.ru/a/b") and is_avito_url("https://avito.ru/x")
    assert not is_avito_url("https://evil.com/a") and not is_avito_url("https://avito.ru.evil.com/a")
    assert not is_avito_url("https://avito.ru@evil.com/a") and not is_avito_url("")


def test_offsite_links_dropped():
    cards = (
        '<div data-marker="item" data-item-id="1"><a data-marker="item-title" href="https://evil.com/x_1234567">'
        '<h3 itemprop="name">Чужой</h3></a></div>'
        '<div data-marker="item" data-item-id="2"><a data-marker="item-title" href="/x/y/t_2">'
        '<h3 itemprop="name">Свой</h3></a></div>'
    )
    assert [c.title for c in parse_search_html(f"<html>{cards}</html>")] == ["Свой"]
    page = '<span data-marker="item-view/total-views">5 просмотров</span><a data-marker="seller-link/link" href="https://evil.com/u">s</a>'
    assert parse_item_page(page).seller_url is None
    sub = (
        '<div data-marker="rubricator"><a data-marker="x/clickable" href="https://evil.com/all/sec/sub-H">Sub</a></div>'
    )
    assert parse_subcategories(sub, "sec", 5) == []


async def test_scanner_block_alert_escaped(tmp_path):
    from tests.test_scanner import setup as scan_setup

    t = scan_setup(tmp_path)
    t.provider.blocked = True

    async def fail(search, page=1):
        raise ProviderBlocked("<b>Доступ & ограничен</b>")

    t.provider.search = fail
    await t.scanner.run_watch_rules(force=True)
    alert = t.notifier.texts[0]
    assert "&lt;b&gt;Доступ &amp; ограничен&lt;/b&gt;" in alert and "<b>Доступ" not in alert
    assert htmllib.unescape(alert).count("<b>") == 1


# --- Даты ---


def test_29_february_uses_previous_leap_year():
    now = datetime(2026, 10, 3, 15, 0)
    assert parse_published("29 февраля", now) == datetime(2024, 2, 29)
    assert parse_published("29 февраля 2023", now) is None
    assert parse_published("25 декабря", now) == datetime(2025, 12, 25)


def test_default_now_is_moscow_naive():
    from app.providers.avito_parser import msk_now

    n = msk_now()
    assert n.tzinfo is None and abs((n - datetime.now()).total_seconds()) < 24 * 3600
