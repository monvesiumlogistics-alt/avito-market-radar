"""MARKET RADAR v1 (ADR-017): что изменилось на рынке — по истории выдачи, без карточек.

Чистые функции (темп категории, тренд, уверенность, модели) + сборка отчёта из БД. Агрегаты считаются на лету из
scan_categories/ads: при нашем объёме (десятки тысяч объявлений) отдельная таблица category_day не нужна.
"""

import html
import statistics
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import Ad, Category, ScanCategory
from app.services.market_logic import icon, pack_messages

FULL_STOPS = ("known", "age_limit", "empty")  # после такого обхода новые с прошлого раза посчитаны целиком
TREND_MIN_DAYS = 3  # раньше о тренде категории не говорим
TREND_WINDOW = 14  # норма — медиана темпа за столько дней
GROW_RATIO, COOL_RATIO = 1.5, 0.6
MIN_PER_DAY = 10  # тише — шум, а не тренд
SIGMA = 3  # отклонение от нормы больше 3√норма (пуассоновский шум)
CONF_NEED = 20  # новых в сутки для полной уверенности по объёму
CONF_FULL_DAYS = 14
EMERGING_MIN_DAYS = 7  # «новые модели» — когда есть с чем сравнивать
TOP_N = 8


def label(conf: float) -> str:
    return "HIGH" if conf >= 0.7 else "MEDIUM" if conf >= 0.4 else "LOW"


def confidence(n: float, need: float, history_days: float, coverage: float, persistent: bool = True) -> float:
    """Слабое звено: объём, длина истории, покрытие; сигнал не второй день подряд — ×0.8."""
    conf = min(min(1.0, n / need), min(1.0, history_days / CONF_FULL_DAYS), coverage)
    return conf * (1.0 if persistent else 0.8)


# --- темп новых объявлений категории ---


def scan_velocity(scan, prev) -> tuple[float | None, float]:
    """(новых в сутки, покрытие 0..1). Есть прошлый обход и дочитали до знакомого/недели — точный счёт новых;
    иначе (первый обход, лимит страниц) — плотность: объявлений на прочитанных страницах / их окно времени."""
    hours = (scan.at - prev.at).total_seconds() / 3600 if prev is not None else None
    if hours is not None and hours < 1:
        return None, 0.0
    if hours is not None and scan.stop_reason in FULL_STOPS:
        return scan.new_ads * 24 / hours, 1.0
    if scan.window_hours < 1:
        return None, 0.0
    coverage = min(1.0, scan.window_hours / hours) if hours else 1.0
    return scan.cards_seen * 24 / scan.window_hours, coverage


@dataclass
class DayPoint:
    day: date
    velocity: float
    coverage: float
    total: int | None


def daily_points(scans: Sequence) -> list[DayPoint]:
    """Обходы категории (любого вида) -> точка на день: новые за день суммируются, темп — к последнему обходу
    предыдущего дня."""
    by_day: dict[date, list] = defaultdict(list)
    for s in sorted(scans, key=lambda s: s.at):
        by_day[s.at.date()].append(s)
    points, prev = [], None
    for day in sorted(by_day):
        group = by_day[day]
        last = group[-1]
        merged = _Merged(
            at=last.at,
            new_ads=sum(s.new_ads for s in group),
            cards_seen=sum(s.cards_seen for s in group),
            window_hours=max(s.window_hours for s in group),
            stop_reason="depth_cap" if any(s.stop_reason == "depth_cap" for s in group) else last.stop_reason,
        )
        v, cov = scan_velocity(merged, prev)
        if v is not None:
            total = next((s.total_count for s in reversed(group) if s.total_count is not None), None)
            points.append(DayPoint(day, v, cov, total))
        prev = merged
    return points


@dataclass
class _Merged:
    at: datetime
    new_ads: int
    cards_seen: int
    window_hours: float
    stop_reason: str


@dataclass
class Trend:
    state: str  # growing | cooling | stable | unknown
    today: float = 0
    base: float = 0
    ratio: float = 0
    history_days: int = 0
    coverage: float = 0
    total: int | None = None
    total_change: float | None = None
    conf: float = 0


def _state(points: list[DayPoint], today: date) -> Trend:
    if not points or points[-1].day != today:
        return Trend("unknown")
    cur, prior = points[-1], [p for p in points[:-1] if p.day >= today - timedelta(days=TREND_WINDOW)]
    t = Trend("unknown", today=cur.velocity, history_days=(today - points[0].day).days, coverage=cur.coverage)
    t.total = cur.total
    ref = next((p.total for p in points if p.total is not None and p.day >= today - timedelta(days=7)), None)
    if ref and cur.total is not None and points[0].day != today:
        t.total_change = cur.total / ref - 1
    if len(prior) < TREND_MIN_DAYS:
        return t
    t.base = statistics.median(p.velocity for p in prior)
    t.ratio = cur.velocity / t.base if t.base else 0
    noise = SIGMA * t.base**0.5
    if cur.velocity >= MIN_PER_DAY and t.ratio >= GROW_RATIO and cur.velocity >= t.base + noise:
        t.state = "growing"
    elif t.base >= MIN_PER_DAY and t.ratio <= COOL_RATIO and cur.velocity <= t.base - noise:
        t.state = "cooling"
    else:
        t.state = "stable"
    return t


def category_trend(scans: Sequence, now: datetime) -> Trend:
    points = daily_points(scans)
    t = _state(points, now.date())
    if t.state in ("growing", "cooling"):
        yesterday = _state(points[:-1], now.date() - timedelta(days=1))
        n = max(t.today, t.base)
        t.conf = confidence(n, CONF_NEED, t.history_days, t.coverage, persistent=yesterday.state == t.state)
    return t


# --- модели ---


@dataclass
class ModelStat:
    key: str
    count: int
    cities: int
    shops: int = 0
    price: int | None = None
    category_id: int | None = None
    prior: int = 0


def _city(url_path: str) -> str:
    return url_path.strip("/").split("/")[0]


def _stat(key: str, ads: Sequence) -> ModelStat:
    prices = [a.price for a in ads if a.price]
    return ModelStat(
        key=key,
        count=len(ads),
        cities=len({_city(a.url_path) for a in ads}),
        shops=len({a.shop for a in ads if a.shop}),
        price=round(statistics.median(prices)) if prices else None,
        category_id=Counter(a.category_id for a in ads).most_common(1)[0][0],
    )


def _by_key(ads: Sequence, since: datetime, until: datetime | None = None) -> dict[str, list]:
    out: dict[str, list] = defaultdict(list)
    for a in ads:
        if a.posted >= since and (until is None or a.posted < until):
            out[a.model_key].append(a)
    return out


def frequent_models(ads: Sequence, now: datetime, days: int = 7) -> list[ModelStat]:
    """Модели, которые за неделю выложили многие: ≥3 объявления минимум в 2 городах; больше городов — выше."""
    stats = [_stat(k, v) for k, v in _by_key(ads, now - timedelta(days=days)).items()]
    stats = [m for m in stats if m.count >= 3 and m.cities >= 2]
    return sorted(stats, key=lambda m: (m.cities, m.count), reverse=True)


def emerging_models(ads: Sequence, now: datetime) -> list[ModelStat]:
    """Новый товарный кластер: за 3 дня ≥5 объявлений в ≥3 городах, а за 2 недели до этого ≤2."""
    split = now - timedelta(days=3)
    prior = _by_key(ads, now - timedelta(days=17), split)
    out = []
    for key, recent in _by_key(ads, split).items():
        m = _stat(key, recent)
        m.prior = len(prior.get(key, []))
        if m.count >= 5 and m.cities >= 3 and m.prior <= 2:
            out.append(m)
    return sorted(out, key=lambda m: (m.cities, m.count), reverse=True)


# --- отчёт ---


def _num(n: float) -> str:
    return f"{round(n):,}".replace(",", " ")


def _pct(x: float) -> str:
    return f"{x * 100:+.0f}%"


def _cities(n: int) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return f"{n} город"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return f"{n} города"
    return f"{n} городов"


def _cat_link(c: Category) -> str:
    sep = "&" if "?" in c.url else "?"
    return f'<a href="{html.escape(c.url + sep + "s=104")}">{html.escape(c.name)}</a>'


def _model_link(key: str) -> str:
    url = f"https://www.avito.ru/rossiya?q={quote(key)}&s=104"
    return f'<a href="{html.escape(url)}">{html.escape(key.title())}</a>'


def trend_line(c: Category, t: Trend) -> str:
    parts = [f"новых <code>{_num(t.today)}</code>/сут (норма {_num(t.base)}, ×{t.ratio:.1f})"]
    if t.total is not None:
        parts.append(f"на полке {_num(t.total)}" + (f" ({_pct(t.total_change)})" if t.total_change is not None else ""))
    return f"• {_cat_link(c)} — " + " · ".join(parts) + f" · {label(t.conf)}"


def model_line(m: ModelStat, cats: dict[int, Category], emerging: bool = False) -> str:
    head = f"{m.count} за 3 дн (было {m.prior})" if emerging else f"{m.count} объявл."
    parts = [head, _cities(m.cities)]
    if m.shops:
        parts.append(f"магазинов {m.shops}")
    if m.price:
        parts.append(f"~{_num(m.price)} ₽")
    cat = cats.get(m.category_id)
    tail = f" · {html.escape(cat.name)}" if cat else ""
    return f"• {_model_link(m.key)} — " + " · ".join(parts) + tail


def build_radar(
    db: Session, now: datetime, cats: Sequence[Category], tail: str | None = None, premium: bool = False
) -> list[str]:
    """Отчёт одним сообщением (длинный — несколькими). tail — телеметрия обхода в свёрнутой цитате."""
    by_id = {c.id: c for c in cats}
    since = now - timedelta(days=30)
    scans: dict[int, list] = defaultdict(list)
    for s in db.scalars(
        select(ScanCategory).where(
            ScanCategory.done.is_(True), ScanCategory.at >= since, ScanCategory.category_id.in_(list(by_id))
        )
    ):
        scans[s.category_id].append(s)
    first = db.scalar(select(func.min(ScanCategory.at)).where(ScanCategory.done.is_(True)))
    history = (now.date() - first.date()).days if first else 0
    today = [cid for cid, ss in scans.items() if any(s.at.date() == now.date() for s in ss)]
    new_today = sum(s.new_ads for cid in today for s in scans[cid] if s.at.date() == now.date())

    trends = {cid: category_trend(ss, now) for cid, ss in scans.items()}
    moving = [(by_id[cid], t) for cid, t in trends.items() if t.state in ("growing", "cooling")]
    strong = [(c, t) for c, t in moving if t.conf >= 0.4]
    weak = [(c, t) for c, t in moving if t.conf < 0.4]

    posted = func.coalesce(Ad.posted_at, Ad.first_seen_at)
    ads = [
        _AdRow(*r)
        for r in db.execute(
            select(Ad.model_key, Ad.url_path, Ad.price, Ad.shop, Ad.category_id, posted).where(
                Ad.model_key.is_not(None), posted >= now - timedelta(days=17), Ad.category_id.in_(list(by_id))
            )
        )
    ]
    frequent = frequent_models(ads, now)[:TOP_N]
    emerging = emerging_models(ads, now)[:TOP_N] if history >= EMERGING_MIN_DAYS else []

    i = lambda e: icon(e, premium)  # noqa: E731
    head = [
        f"<b>{i('📡')} MARKET RADAR · {now:%d.%m}</b>",
        f"Покрытие: <code>{len(today)} / {len(cats)}</code> категорий · новых объявлений <code>{_num(new_today)}</code>"
        f" · история <code>{history}</code> дн",
    ]
    if history < TREND_MIN_DAYS:
        head.append(f"⏳ Тренды категорий появятся после {TREND_MIN_DAYS} дней истории (сейчас {history}).")
    blocks = ["\n".join(head)]
    up = sorted((x for x in strong if x[1].state == "growing"), key=lambda x: -x[1].ratio)[:TOP_N]
    down = sorted((x for x in strong if x[1].state == "cooling"), key=lambda x: x[1].ratio)[:TOP_N]
    if up:
        blocks.append(f"<b>{i('🔥')} Предложение растёт</b>\n" + "\n".join(trend_line(c, t) for c, t in up))
    if down:
        blocks.append("<b>🧊 Остывает</b>\n" + "\n".join(trend_line(c, t) for c, t in down))
    if emerging:
        blocks.append(
            f"<b>{i('🚀')} Новые модели</b>\n" + "\n".join(model_line(m, by_id, emerging=True) for m in emerging)
        )
    if frequent:
        lines = "\n".join(model_line(m, by_id) for m in frequent)
        blocks.append(f"<b>{i('🔁')} Модели у многих продавцов · 7 дн</b>\n{lines}")
    if weak:
        lines = "\n".join(trend_line(c, t) for c, t in sorted(weak, key=lambda x: -x[1].ratio)[:TOP_N])
        blocks.append(f"<blockquote expandable>👀 Наблюдаю (мало данных)\n{lines}</blockquote>")
    if tail:
        blocks.append(f"<blockquote expandable>{tail}</blockquote>")
    return pack_messages(blocks)


@dataclass
class _AdRow:
    model_key: str
    url_path: str
    price: int | None
    shop: str | None
    category_id: int
    posted: datetime
