"""ADR-018: блок без капчи — пауза и продолжение без человека (60 → 120 → 240 мин), ровный темп обхода."""

import asyncio

from app.db import CrawlRun, ScanCategory
from app.providers.base import ProviderBlocked
from tests.test_history import counted
from tests.test_market import life, rows, search_html, url_of


def flaky(n_blocks: int, html: str):
    """Страница, которая первые n_blocks загрузок отвечает блоком (HTTP 439), потом нормально."""
    state = {"left": n_blocks}

    def page():
        if state["left"] > 0:
            state["left"] -= 1
            raise ProviderBlocked("Avito: HTTP 439")
        return html

    return page


def cooldown_life(tmp_path, n_blocks, **settings):
    t = life(tmp_path, {"A": [900]}, **({"captcha_wait_minutes": 0} | settings))
    t.pages[url_of("Ac1")] = flaky(n_blocks, search_html(("A1", "Item A 1", 20000, "1 день назад")))
    t.crawler.cooldown_unit = 0.001  # «минута» паузы в тестах — 1 мс
    return t


async def test_block_pauses_and_resumes_without_human(tmp_path):
    t = cooldown_life(tmp_path, 2, block_cooldowns=3)
    t.crawler.start("sweep")
    await t.crawler._task
    (run,) = rows(t, CrawlRun)
    assert run.status == "done"
    pauses = [x for x in t.notifier.sent if x.startswith("⏸")]
    assert len(pauses) == 2 and "<code>60</code> мин" in pauses[0] and "<code>120</code> мин" in pauses[1]
    assert t.crawler.blocks == 2
    assert "⏸ Пауз после блока: <code>2</code>" in t.notifier.sent[-1]  # телеметрия в итоге


async def test_block_gives_up_after_cooldowns(tmp_path):
    t = cooldown_life(tmp_path, 10, block_cooldowns=2)
    t.crawler.start("sweep")
    await t.crawler._task
    (run,) = rows(t, CrawlRun)
    assert run.status == "blocked"
    assert len([x for x in t.notifier.sent if x.startswith("⏸")]) == 2


async def test_stop_during_pause(tmp_path):
    t = cooldown_life(tmp_path, 10, block_cooldowns=3)
    t.crawler.cooldown_unit = 60
    t.crawler.cooldown_poll = 0.01
    t.crawler.start("sweep")
    while not any(x.startswith("⏸") for x in t.notifier.sent):
        await asyncio.sleep(0.01)
    t.crawler.stop()
    await t.crawler._task
    assert rows(t, CrawlRun)[0].status == "stopped"



async def test_human_pass_then_quiet_minutes_and_slow_mode(tmp_path):
    t = cooldown_life(tmp_path, 1, captcha_wait_minutes=15, post_captcha_cooldown_min=5)
    t.provider.human = True
    t.crawler.start("sweep")
    await t.crawler._task
    assert rows(t, CrawlRun)[0].status == "done"
    assert t.crawler.traffic.today()["captcha"] == 1 and t.crawler.traffic.slow  # 2 ч в замедленном темпе


async def test_blocks_per_day_limit_stops_run(tmp_path):
    t = cooldown_life(tmp_path, 10, block_cooldowns=5, max_blocks_per_day=2)
    t.crawler.start("sweep")
    await t.crawler._task
    assert rows(t, CrawlRun)[0].status == "blocked"
    assert len([x for x in t.notifier.sent if x.startswith("⏸")]) == 1  # 1 пауза, 2-й блок — стоп до завтра
    assert t.crawler.traffic.today()["block"] == 2


async def test_large_category_sampled_one_page(tmp_path):
    t = life(tmp_path, {"A": [900]})
    page = counted(search_html(("A1", "Item A 1", 20000, "1 день назад")), 115000)
    t.pages[url_of("Ac1")] = page
    t.pages[url_of("Ac1") + "&p=2"] = page
    t.crawler.start("sweep")
    await t.crawler._task
    (sc,) = rows(t, ScanCategory)
    assert (sc.pages, sc.stop_reason, sc.total_count) == (1, "sample", 115000)
    assert url_of("Ac1") + "&p=2" not in t.provider.calls
