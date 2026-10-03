"""Единая точка темпа запросов к Avito (ADR-019): «не чаще раза в N с», не больше X в час и Y в сутки, замедление
после капчи, лимит блоков в сутки. Суточные счётчики — в SQLite (переживают перезапуск).

Только замедляет: ускорения ниже базового интервала нет — репутация адреса копится часами (docs/avito-traffic-v2.md).
"""

import asyncio
import random
import time
from collections import deque
from collections.abc import Callable
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import TrafficDay as TrafficRow

COUNTED = ("search", "card", "profile", "recheck", "monitor")  # загрузки страниц; block/captcha — события


class TrafficLimit(Exception):
    """На сегодня хватит: reason = 'budget' (суточный лимит загрузок) или 'blocks' (слишком много блоков)."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


class AvitoTraffic:
    def __init__(
        self,
        session_factory: sessionmaker,
        settings: Settings,
        clock: Callable[[], datetime] = datetime.now,
        sleep=asyncio.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ):
        self.session_factory, self.s, self.clock = session_factory, settings, clock
        self.sleep, self.monotonic = sleep, monotonic
        self._last: float | None = None
        self._hour: deque[float] = deque()
        self.slow_until: datetime | None = None
        self.waited = 0.0  # секунд ожидания из-за темпа (телеметрия)

    # --- суточные счётчики ---

    def today(self, day: date | None = None) -> dict[str, int]:
        day = day or self.clock().date()
        with self.session_factory() as db:
            return {r.kind: r.count for r in db.scalars(select(TrafficRow).where(TrafficRow.day == day))}

    def _add(self, kind: str) -> None:
        day = self.clock().date()
        with self.session_factory() as db:
            row = db.get(TrafficRow, (day, kind)) or TrafficRow(day=day, kind=kind, count=0)
            row.count += 1
            db.merge(row)
            db.commit()

    def loads_today(self) -> int:
        t = self.today()
        return sum(t.get(k, 0) for k in COUNTED)

    # --- темп ---

    @property
    def slow(self) -> bool:
        return self.slow_until is not None and self.clock() < self.slow_until

    async def _wait(self, seconds: float, cancel: Callable[[], bool] | None) -> bool:
        """Ждать, проверяя отмену; False — отменили."""
        end = self.monotonic() + seconds
        while (left := end - self.monotonic()) > 0:
            if cancel and cancel():
                return False
            await self.sleep(min(left, 5))
            self.waited += min(left, 5)
        return not (cancel and cancel())

    async def acquire(self, kind: str, cancel: Callable[[], bool] | None = None) -> bool:
        """Перед каждой загрузкой. False — отменили (/stop); TrafficLimit — на сегодня всё."""
        t = self.today()
        if t.get("block", 0) >= self.s.max_blocks_per_day:
            raise TrafficLimit("blocks")
        if sum(t.get(k, 0) for k in COUNTED) >= self.s.avito_daily_budget:
            raise TrafficLimit("budget")
        while len(self._hour) >= self.s.avito_max_per_hour:
            if self._hour[0] <= self.monotonic() - 3600:
                self._hour.popleft()
                continue
            if not await self._wait(self._hour[0] + 3600 - self.monotonic(), cancel):
                return False
        if self._last is not None:
            interval = self.s.avito_min_interval_s * (self.s.slow_factor if self.slow else 1)
            gap = self._last + interval * random.uniform(0.5, 1.5) - self.monotonic()
            if gap > 0 and not await self._wait(gap, cancel):
                return False
        now = self.monotonic()
        self._last = now
        self._hour.append(now)
        self._add(kind)
        return True

    # --- блоки ---

    def on_block(self) -> bool:
        """Учесть блок. True — лимит блоков на сутки исчерпан: дальше не идём."""
        self._add("block")
        return self.today().get("block", 0) >= self.s.max_blocks_per_day

    def on_human_passed(self) -> float:
        """Капча пройдена: SLOW_HOURS в замедленном темпе; вернуть, сколько секунд выждать без загрузок."""
        self._add("captcha")
        self.slow_until = self.clock() + timedelta(hours=self.s.slow_hours)
        return self.s.post_captcha_cooldown_min * 60

    def on_pause_done(self) -> None:
        """После паузы без человека — тоже замедленный темп."""
        self.slow_until = self.clock() + timedelta(hours=self.s.slow_hours)

    def summary(self) -> str:
        t = self.today()
        parts = " · ".join(f"{k} {t[k]}" for k in COUNTED if t.get(k))
        line = f"Загрузки сегодня: <code>{self.loads_today()} / {self.s.avito_daily_budget}</code>"
        line += f" ({parts})" if parts else ""
        extra = [f"{label} {t[k]}" for k, label in (("block", "блоков"), ("captcha", "капч")) if t.get(k)]
        if self.slow:
            extra.append(f"медленный режим до {self.slow_until:%H:%M}")
        return line + (" · " + " · ".join(extra) if extra else "")
