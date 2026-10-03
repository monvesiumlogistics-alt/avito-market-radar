"""ADR-019: единый темп запросов к Avito — интервал, час, сутки, блоки, замедление после капчи."""

from datetime import datetime, timedelta

import pytest

from app.config import Settings
from app.db import init_db
from app.providers.traffic import AvitoTraffic, TrafficLimit

NOW = datetime(2026, 10, 4, 9, 0)


class Clock:
    """Фальшивое время: sleep двигает и монотонные, и настенные часы."""

    def __init__(self):
        self.mono, self.wall, self.slept = 0.0, NOW, []

    async def sleep(self, s):
        self.slept.append(s)
        self.mono += s
        self.wall += timedelta(seconds=s)


def make(tmp_path, **kw):
    c = Clock()
    s = Settings(_env_file=None, **kw)
    tr = AvitoTraffic(init_db(f"sqlite:///{tmp_path}/t.db"), s, clock=lambda: c.wall, sleep=c.sleep,
                      monotonic=lambda: c.mono)  # fmt: skip
    return tr, c


async def test_min_interval_with_jitter(tmp_path, monkeypatch):
    monkeypatch.setattr("app.providers.traffic.random.uniform", lambda a, b: 1.0)
    tr, c = make(tmp_path, avito_min_interval_s=45)
    assert await tr.acquire("search")
    assert c.mono == 0  # первая — сразу
    assert await tr.acquire("card")
    assert c.mono == 45
    c.mono += 100  # давно не ходили — без ожидания
    before = c.mono
    assert await tr.acquire("search")
    assert c.mono == before
    assert tr.today() == {"search": 2, "card": 1} and tr.loads_today() == 3


async def test_hourly_cap(tmp_path):
    tr, c = make(tmp_path, avito_min_interval_s=0, avito_max_per_hour=3)
    for _ in range(3):
        await tr.acquire("search")
    assert c.mono == 0
    await tr.acquire("search")  # 4-я — ждёт, пока первая выйдет из часового окна
    assert c.mono >= 3600


async def test_daily_budget_survives_restart(tmp_path):
    tr, c = make(tmp_path, avito_min_interval_s=0, avito_daily_budget=2)
    await tr.acquire("search")
    await tr.acquire("card")
    with pytest.raises(TrafficLimit) as e:
        await tr.acquire("search")
    assert e.value.reason == "budget"
    again = AvitoTraffic(tr.session_factory, tr.s, clock=lambda: c.wall, sleep=c.sleep, monotonic=lambda: c.mono)
    with pytest.raises(TrafficLimit):  # рестарт бота не обнуляет сутки
        await again.acquire("search")
    c.wall += timedelta(days=1)
    assert await again.acquire("search")  # новые сутки


async def test_blocks_per_day_stop(tmp_path):
    tr, _ = make(tmp_path, avito_min_interval_s=0, max_blocks_per_day=2)
    assert tr.on_block() is False
    assert tr.on_block() is True
    with pytest.raises(TrafficLimit) as e:
        await tr.acquire("search")
    assert e.value.reason == "blocks"


async def test_captcha_cooldown_and_slow_mode(tmp_path, monkeypatch):
    monkeypatch.setattr("app.providers.traffic.random.uniform", lambda a, b: 1.0)
    tr, c = make(tmp_path, avito_min_interval_s=40, post_captcha_cooldown_min=5, slow_factor=2, slow_hours=2)
    await tr.acquire("search")
    assert tr.on_human_passed() == 300 and tr.slow
    await tr.acquire("search")
    assert c.mono == 80  # интервал ×2
    c.wall += timedelta(hours=2, minutes=1)
    c.mono += 1000
    assert not tr.slow
    t0 = c.mono
    await tr.acquire("search")
    assert c.mono == t0  # нормальный темп вернулся
    assert tr.today()["captcha"] == 1


async def test_cancel_during_wait(tmp_path):
    tr, c = make(tmp_path, avito_min_interval_s=1000)
    await tr.acquire("search")
    stop = {"v": False}

    async def sleep(s):
        stop["v"] = True  # /stop пришёл, пока ждём
        c.mono += s

    tr.sleep = sleep
    assert await tr.acquire("search", cancel=lambda: stop["v"]) is False
    assert tr.today() == {"search": 1}  # отменённая загрузка не учтена


def test_summary_line(tmp_path):
    tr, _ = make(tmp_path)
    tr._add("search")
    tr._add("block")
    assert tr.summary() == "Загрузки сегодня: <code>1 / 450</code> (search 1) · блоков 1"
