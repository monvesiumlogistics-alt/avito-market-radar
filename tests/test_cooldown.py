"""ADR-018: блок без капчи — пауза и продолжение без человека (60 → 120 → 240 мин), ровный темп обхода."""

import asyncio

from app.db import CrawlRun
from app.providers.base import ProviderBlocked
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
    t = life(tmp_path, {"A": [900]}, captcha_wait_minutes=0, **settings)
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


async def test_sweep_pause_between_loads(tmp_path, monkeypatch):
    slept = []

    async def fake_sleep(s):
        slept.append(s)

    t = life(tmp_path, {"A": [900]})
    t.crawler.sweep_pause, t.crawler.kind = 10, "sweep"
    monkeypatch.setattr("app.services.market.asyncio.sleep", fake_sleep)
    await t.crawler._sweep_pause()
    assert len(slept) == 1 and 5 <= slept[0] <= 15  # 10 с ± 50%
    t.crawler.kind = "report"
    await t.crawler._sweep_pause()
    assert len(slept) == 1  # /report — без доп. паузы
