"""Ежедневный обход выдачи (ADR-016): чистые функции плана, расписания и сводки. Загрузки — в MarketCrawler.

Идея — информация на загрузку: листаем ровно столько страниц, сколько в категории могло появиться нового
с прошлого раза, останавливаемся на уже знакомом, тихие категории смотрим через день.
"""

import math
import statistics
from collections import Counter
from collections.abc import Sequence
from datetime import datetime

from app.config import Settings
from app.services.market_logic import icon

PAGE_SIZE = 50  # объявлений на странице выдачи
DEPTH_MARGIN = 1.3  # запас на неравномерность потока
RATE_SCANS = 7  # по скольким последним обходам категории оценивать темп
QUIET_REVISIT_HOURS = 36  # тихую категорию, обойдённую позже этого, сегодня пропускаем
DONE_STATUSES = ("done", "budget")  # такой обход за сегодня — повторять не нужно

STOP_LABELS = {
    "known": "знакомые", "age_limit": "неделя", "depth_cap": "лимит глубины", "empty": "пусто", "sample": "выборка",
}


def rate_per_hour(scans: Sequence) -> float | None:
    """Новых объявлений в час: медиана cards_seen / window_hours последних обходов (окно < 1 ч не считается)."""
    rates = [s.cards_seen / s.window_hours for s in scans[:RATE_SCANS] if s.window_hours >= 1]
    return statistics.median(rates) if rates else None


def plan_depth(rate: float | None, hours_since: float | None, s: Settings) -> int:
    """Сколько страниц читать: ожидаемое число новых с прошлого обхода / 50 × запас + 1 страница на стык."""
    if rate is None or hours_since is None:
        return s.sweep_first_pages
    if rate * 24 > s.sweep_large_per_day:
        return 1  # огромная категория: только выборка 1-й страницы (ADR-019)
    pages = math.ceil(rate * hours_since / PAGE_SIZE * DEPTH_MARGIN) + 1
    return max(2, min(s.sweep_max_pages, pages))


def is_quiet(rate: float | None, hours_since: float | None, s: Settings) -> bool:
    """Тихая категория (< SWEEP_QUIET_PER_DAY новых в сутки), обойдённая недавно: сегодня пропустить."""
    return rate is not None and hours_since is not None and rate * 24 < s.sweep_quiet_per_day and (
        hours_since < QUIET_REVISIT_HOURS
    )


def daily_due(now: datetime, at: str, last_sweep) -> bool:
    """Пора ли ежедневный обход: время наступило и сегодня ещё нет завершённого (done/budget) обхода."""
    if not at:
        return False
    hour, minute = (int(x) for x in at.split(":"))
    if now < now.replace(hour=hour, minute=minute, second=0, microsecond=0):
        return False
    return not (last_sweep and last_sweep.started_at.date() == now.date() and last_sweep.status in DONE_STATUSES)


def _num(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def format_sweep_summary(
    day: str,
    rows: Sequence,
    total: int,
    quiet: int,
    loads: int,
    captcha_waits: int,
    errors: Sequence[str],
    status: str,
    note: str | None = None,
    premium: bool = False,
    pauses: int = 0,
    traffic: str | None = None,
) -> str:
    """rows: ScanCategory за прогон с именем категории (.name). Телеметрия обхода одним сообщением."""
    stops = Counter(r.stop_reason for r in rows)
    lines = [
        f"<b>{icon('📡', premium)} Обход рынка · {day}</b>",
        "",
        f"{icon('📂', premium)} Категорий: <code>{len(rows)} / {total}</code> · тихих пропущено: <code>{quiet}</code>",
        f"{icon('⏩', premium)} Страниц: <code>{sum(r.pages for r in rows)}</code>"
        f" · всего загрузок: <code>{loads}</code>",
        f"{icon('✅', premium)} Новых объявлений: <code>{_num(sum(r.new_ads for r in rows))}</code>"
        f" · знакомых: <code>{_num(sum(r.known_ads for r in rows))}</code>",
        "Остановки: " + (" · ".join(f"{STOP_LABELS.get(k, k)} {v}" for k, v in stops.most_common()) or "—"),
    ]
    if captcha_waits:
        lines.append(f"🧩 Капча: <code>{captcha_waits}</code>")
    if pauses:
        lines.append(f"⏸ Пауз после блока: <code>{pauses}</code>")
    if traffic:
        lines.append(traffic)
    if errors:
        lines.append(f"{icon('❗️', premium)} Ошибок: <code>{len(errors)}</code>")
    short = sorted((r for r in rows if r.stop_reason == "depth_cap"), key=lambda r: r.window_hours)
    if short:
        lines.append(
            "⚠️ Не досмотрено (лимит страниц): " + ", ".join(f"{r.name} — {r.window_hours:.0f} ч" for r in short[:8])
        )
    if note:
        lines += ["", note]
    return "\n".join(lines)
