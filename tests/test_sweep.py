"""ADR-016: ежедневный инкрементальный обход выдачи (sweep) — глубина по темпу категории, стоп на знакомом,
тихие категории через день, чекпойнт по страницам, отдельный бюджет, расписание 09:00 + догоняющий запуск."""

from datetime import datetime, timedelta
from types import SimpleNamespace as NS

from app.config import Settings
from app.db import CrawlRun, ScanCategory
from app.providers.base import ProviderBlocked
from app.services.sweep import daily_due, format_sweep_summary, is_quiet, plan_depth, rate_per_hour
from tests.test_handlers import make, send
from tests.test_market import NOW, life, rows, search_html, url_of

S = Settings(_env_file=None)


def scan(cards, hours, at=NOW):
    return NS(cards_seen=cards, window_hours=hours, at=at)


# --- чистые функции ---


def test_rate_per_hour_is_median_of_recent_scans():
    assert rate_per_hour([]) is None
    assert rate_per_hour([scan(5, 0.5)]) is None  # окно < 1 ч — не оценка
    assert rate_per_hour([scan(240, 24), scan(10, 10), scan(500, 10)]) == 10  # медиана устойчива к выбросу


def test_plan_depth_by_expected_new_listings():
    assert plan_depth(None, None, S) == S.sweep_first_pages  # истории нет
    assert plan_depth(10, 24, S) == 8  # 240 новых ≈ 4.8 стр. × 1.3 запас → 7, +1 на стык
    assert plan_depth(0.5, 24, S) == 2
    assert plan_depth(0.01, 24, S) == 2  # минимум: страница + стык
    assert plan_depth(100, 24, S) == S.sweep_max_pages


def test_quiet_category_swept_every_other_day():
    assert is_quiet(0.5, 20, S)  # 12/сутки, вчера были — сегодня пропуск
    assert not is_quiet(0.5, 40, S)  # позавчера — пора
    assert not is_quiet(5, 20, S)  # 120/сутки — каждый день
    assert not is_quiet(None, 20, S)


def test_daily_due_after_time_once_per_day():
    at = "09:00"
    day = datetime(2026, 10, 4)
    assert not daily_due(day.replace(hour=8, minute=59), at, None)  # ещё рано
    assert daily_due(day.replace(hour=9), at, None)
    assert daily_due(day.replace(hour=13), at, NS(started_at=day - timedelta(hours=20), status="done"))  # вчерашний
    assert not daily_due(day.replace(hour=13), at, NS(started_at=day.replace(hour=9), status="done"))
    assert not daily_due(day.replace(hour=13), at, NS(started_at=day.replace(hour=9), status="budget"))
    assert daily_due(day.replace(hour=13), at, NS(started_at=day.replace(hour=9), status="blocked"))  # продолжить
    assert not daily_due(day.replace(hour=13), "", None)  # расписание выключено


def test_summary_text_counts_and_coverage():
    rows_ = [
        NS(name="A", pages=3, new_ads=120, known_ads=10, stop_reason="depth_cap", window_hours=8, cards_seen=130),
        NS(name="B", pages=1, new_ads=2, known_ads=40, stop_reason="known", window_hours=30, cards_seen=42),
    ]
    text = format_sweep_summary("04.10", rows_, total=5, quiet=1, loads=6, captcha_waits=1, errors=[], status="done")
    assert "<b>📡 Обход рынка · 04.10</b>" in text
    assert "Категорий: <code>2 / 5</code> · тихих пропущено: <code>1</code>" in text
    assert "Новых объявлений: <code>122</code> · знакомых: <code>50</code>" in text
    assert "знакомые 1" in text and "лимит глубины 1" in text
    assert "A — 8 ч" in text  # не досмотрено
    assert "🧩 Капча: <code>1</code>" in text


# --- прогон ---


async def test_first_sweep_then_next_day_stops_on_known(tmp_path):
    t = life(tmp_path, {"A": [900, 900]})
    assert t.crawler.start("sweep") == "Начинаю обход рынка"
    await t.crawler._task
    (run,) = runs = rows(t, CrawlRun)
    assert (run.kind, run.status) == ("sweep", "done")
    assert run.loads == 5  # warm-up + 2 × (стр. 1 + пустая стр. 2); карточки не открываются
    assert not any("/t_" in u for u in t.provider.calls)
    assert {r.stop_reason for r in rows(t, ScanCategory)} == {"empty"}
    assert "📡 Обход рынка" in t.notifier.sent[-1]

    t.now[0] += timedelta(days=2)  # тихая категория (1 объявление за неделю) — раз в 2 дня
    t.provider.calls.clear()
    t.crawler.start("sweep")
    await t.crawler._task
    runs = rows(t, CrawlRun)
    assert len(runs) == 2 and runs[1].status == "done"
    assert len(t.provider.calls) == 2  # по одной странице: всё знакомо
    second = [r for r in rows(t, ScanCategory) if r.run_id == runs[1].id]
    assert {(r.stop_reason, r.pages, r.new_ads, r.known_ads, r.done) for r in second} == {("known", 1, 0, 1, True)}


async def test_sweep_resumes_from_next_page_after_block(tmp_path):
    t = life(tmp_path, {"A": [900]}, captcha_wait_minutes=0, sweep_first_pages=3)
    page2 = url_of("Ac1") + "&p=2"
    t.pages[page2] = ProviderBlocked("captcha")
    t.crawler.start("sweep")
    await t.crawler._task
    (run,) = rows(t, CrawlRun)
    assert run.status == "blocked"
    (sc,) = rows(t, ScanCategory)
    assert (sc.pages, sc.done, sc.new_ads) == (1, False, 1)

    t.pages[page2] = search_html(("A1b", "Item 2", 20000, "1 день назад"))
    t.provider.calls.clear()
    assert t.crawler.start("sweep").startswith("Продолжаю обход")
    await t.crawler._task
    assert url_of("Ac1") not in t.provider.calls and page2 in t.provider.calls  # страница 1 не перечитана
    (sc,) = rows(t, ScanCategory)
    assert (sc.pages, sc.done, sc.new_ads) == (3, True, 2)  # 1 + 2 + пустая 3


async def test_sweep_and_report_runs_do_not_resume_each_other(tmp_path):
    t = life(tmp_path, {"A": [900]}, captcha_wait_minutes=0)
    t.pages[url_of("Ac1")] = ProviderBlocked("captcha")
    t.crawler.start()
    await t.crawler._task
    assert rows(t, CrawlRun)[0].status == "blocked"
    assert t.crawler.start("sweep") == "Начинаю обход рынка"  # прерванный /report не продолжается обходом
    await t.crawler._task
    assert [r.kind for r in rows(t, CrawlRun)] == ["report", "sweep"]


async def test_sweep_has_own_budget(tmp_path):
    t = life(tmp_path, {"A": [900, 900, 900]}, report_budget=1, sweep_budget=3)
    t.crawler.start("sweep")
    await t.crawler._task
    (run,) = rows(t, CrawlRun)
    assert (run.status, run.loads) == ("budget", 3)


async def test_quiet_category_skipped_next_day(tmp_path):
    t = life(tmp_path, {"A": [900]})
    t.crawler.start("sweep")
    await t.crawler._task  # 1 объявление за 7 дней — тихая
    t.now[0] += timedelta(hours=20)
    t.provider.calls.clear()
    t.crawler.start("sweep")
    await t.crawler._task
    assert len(t.provider.calls) == 0 and "тихих пропущено: <code>1</code>" in t.notifier.sent[-1]


async def test_daily_starts_sweep_once(tmp_path):
    t = life(tmp_path, {"A": [900]}, daily_sweep_at="09:00")
    assert t.crawler.daily() is True
    await t.crawler._task
    assert t.crawler.daily() is False  # сегодня уже был
    t.now[0] = NOW.replace(hour=8) + timedelta(days=1)
    assert t.crawler.daily() is False  # завтра до 09:00


async def test_sweep_command():
    t = make()
    await send(t, "/sweep")
    assert t.crawler.calls == ["sweep"]
