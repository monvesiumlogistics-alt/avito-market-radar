"""Проверка рынка: обход подкатегорий Avito, поиск товаров с высоким спросом (просмотры в день)."""

import asyncio
import contextlib
import logging
import math
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace as NS
from typing import Protocol

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select, update
from sqlalchemy.orm import sessionmaker

from app.config import Settings, split_csv
from app.db import Category, CrawlRun, Find, ScanCategory
from app.models import Listing
from app.providers.avito_parser import (
    BASE_URL,
    SELECTORS,
    is_gone,
    msk_now,
    parse_item_page,
    parse_published,
    parse_search_html,
    parse_seller_date,
    parse_subcategories,
    parse_total_count,
    promoted_ids,
    with_page,
)
from app.providers.base import AvitoProvider, BrowserLost, ProviderBlocked
from app.providers.traffic import AvitoTraffic, TrafficLimit
from app.scope import ACTIVE
from app.services.history import record_card, record_gone, record_search, update_ad
from app.services.market_cmds import entries_for
from app.services.market_logic import (
    age_days,
    calc_vpd,
    crawl_order,
    current_label,
    date_checked,
    dedupe_finds,
    find_age,
    format_progress,
    format_results,
    group_cards,
    icon,
    is_find,
    is_hot,
    market_lines,
    market_stats,
    model_groups,
    norm_title,
    pick_groups,
    stats_lines,
    summary_tail,
)
from app.services.panels import captcha_alert, captcha_passed
from app.services.radar import build_radar
from app.services.scoping import query_matcher
from app.services.sweep import daily_due, format_sweep_summary, is_quiet, plan_depth, rate_per_hour

log = logging.getLogger(__name__)

REPORT_SCOPES = ("CORE", "WATCH", "QUERY")  # /report (карточки) — не по EXPLORE/OFF (ADR-020)
RESUMABLE = ("running", "stopped", "blocked", "interrupted", "failed")  # budget/done: следующий прогон новый
MAX_ERROR_STREAK = 3
RECHECK_MIN_AGE = timedelta(days=2)  # перепроверяем находки возрастом 2-14 дней
RECHECK_MAX_AGE = timedelta(days=14)
STATUS_NOTES = {
    "stopped": "Проверка остановлена, продолжу по /report.",
    "blocked": "⚠️ Avito ограничил доступ, продолжу по /report.",
    "failed": "⚠️ Проверка упала, найденное сохранено.",
    "budget": "Бюджет загрузок исчерпан.",
}

SUBCAT_REFRESH = timedelta(days=30)  # подкатегории раздела перечитываем не чаще
FETCH_TIMEOUT = 120  # с на одну загрузку: зависший Playwright не должен держать браузер (m9)


class MarketNotifier(Protocol):
    async def send_text(self, text: str, markup=None) -> int | None: ...
    async def edit_text(self, message_id: int, text: str) -> None: ...


class StopRequested(Exception):
    """/stop: проверяется перед каждой загрузкой."""


class BudgetExhausted(Exception):
    """Бюджет загрузок прогона исчерпан."""


class BreakerTripped(Exception):
    """Несколько подкатегорий подряд упали с ошибкой: системный сбой, дальше идти бессмысленно."""


@dataclass
class Opened:
    """Результат открытия карточки."""

    card: Listing
    views: int
    today: int | None
    page_date: datetime | None
    age: float
    vpd: int
    seller_url: str | None


class MarketCrawler:
    def __init__(
        self,
        session_factory: sessionmaker,
        provider_factory: Callable[[], AvitoProvider],
        notifier: MarketNotifier,
        settings: Settings,
        gate=None,  # BrowserGate | None; используется с I10/I11
        clock: Callable[[], datetime] = msk_now,
        traffic: AvitoTraffic | None = None,  # общий темп запросов к Avito (ADR-019); общий со сканером
    ):
        self.session_factory = session_factory
        self.provider_factory = provider_factory
        self.notifier = notifier
        self.settings = settings
        self.gate = gate
        self.clock = clock
        self.fetch_timeout: float = FETCH_TIMEOUT
        self.stop_requested = False
        # состояние текущего прогона
        self.provider: AvitoProvider | None = None
        self.run_id: int | None = None
        self.loads = 0  # загрузок с последнего /report: бюджет считается заново на каждом нажатии (ADR-007)
        self._loads_saved = 0  # сколько из них уже добавлено в run.loads (накопительная статистика)
        self.errors: list[str] = []
        self.send_delay = 1.0  # пауза между кусками длинного сообщения (лимиты Telegram); в тестах 0
        self.captcha_poll = 5.0  # с между проверками, прошёл ли человек капчу
        self.reopen_delay = 2.0  # с до повторного запуска браузера (m10)
        self.cooldown_unit = 60.0  # секунд в «минуте» паузы после блока (в тестах меньше)
        self.cooldown_poll = 5.0  # с между проверками /stop во время паузы
        self.traffic = traffic or AvitoTraffic(session_factory, settings, clock)
        self.blocks = 0  # пауз после блока в этом прогоне
        self._task: asyncio.Task | None = None
        self._owns_gate = False
        self._progress_at: datetime | None = None
        self._progress_id: int | None = None
        self._progress_tried = False
        self._progress_last = ""
        self._started: datetime | None = None
        self._current: str | None = None  # что обходится сейчас (строка «Сейчас» в сообщении о прогрессе)
        self._ticker: asyncio.Task | None = None
        self.gone_ids: list[int] = []  # находки, исчезнувшие при перепроверке в этом прогоне
        self.kind = "report"  # report (/report) | sweep (ежедневный обход выдачи, ADR-016)
        self.captcha_waits = 0
        self._quiet = 0  # тихих категорий, пропущенных обходом
        self.deep_counts: Counter = Counter()  # Phase 4: открыто карточек по выборкам

    # --- жизненный цикл ---

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def budget(self) -> int:
        if self.kind == "deep":
            return self.settings.deep_budget
        return self.settings.sweep_budget if self.kind == "sweep" else self.settings.report_budget

    def start(self, kind: str = "report") -> str:
        """/report или /sweep: ответ пользователю; обход идёт в фоновой задаче. Продолжается прогон того же вида."""
        if self.running:
            busy = "Проверка уже идёт" if self.kind == "report" else "Обход уже идёт"
            return f"{busy}\n" + self._progress_text()
        now, s = self.clock(), self.settings
        with self.session_factory() as db:
            last = db.scalars(
                select(CrawlRun).where(CrawlRun.kind == kind).order_by(CrawlRun.id.desc()).limit(1)
            ).first()
            resume = (
                last is not None
                and last.status in RESUMABLE
                and last.started_at > now - timedelta(hours=s.report_resume_hours)
            )
            if resume:
                run = last
                run.status, run.finished_at = "running", None
                if kind == "sweep":
                    done = db.scalar(
                        select(func.count())
                        .select_from(ScanCategory)
                        .where(ScanCategory.run_id == run.id, ScanCategory.done.is_(True))
                    )
                    reply = f"Продолжаю обход (пройдено категорий: {done})"
                else:
                    done = db.scalar(
                        select(func.count()).select_from(Category).where(Category.last_run_id == run.id)
                    )
                    reply = f"Продолжаю проверку (пройдено подкатегорий: {done})"
            else:
                run = CrawlRun(started_at=now, kind=kind)
                db.add(run)
                reply = "Начинаю проверку рынка" if kind == "report" else "Начинаю обход рынка"
            db.commit()
            self.run_id, self.loads, self._loads_saved = run.id, 0, 0
        self.kind, self.captcha_waits, self._quiet, self.blocks = kind, 0, 0, 0
        self.deep_counts = Counter()
        self.stop_requested, self.errors = False, []
        self._progress_at = self._progress_id = None
        self.gone_ids = []
        self._started, self._current, self._progress_last = now, None, ""
        self._progress_tried = False  # на продолжении новое сообщение о прогрессе (m6)
        self._task = asyncio.create_task(self._run())
        return reply

    def daily(self) -> bool:
        """По расписанию и при старте бота: ежедневный обход, если пора и сегодня ещё не было (ADR-016)."""
        if self.running:
            return False
        with self.session_factory() as db:
            last = db.scalars(
                select(CrawlRun).where(CrawlRun.kind == "sweep").order_by(CrawlRun.id.desc()).limit(1)
            ).first()
        if not daily_due(self.clock(), self.settings.daily_sweep_at, last):
            return False
        log.info("[MARKET] ежедневный обход: %s", self.start("sweep"))
        return True

    def stop(self) -> str:
        if not self.running:
            return "Проверка не идёт"
        self.stop_requested = True
        return "Останавливаю после текущей страницы…"

    def mark_interrupted(self) -> None:
        """При старте бота: зависший 'running' (бот перезапустили) -> 'interrupted', можно продолжить."""
        with self.session_factory() as db:
            db.execute(
                update(CrawlRun)
                .where(CrawlRun.status == "running")
                .values(status="interrupted", finished_at=self.clock())
            )
            db.commit()

    async def _run(self) -> None:
        status = "interrupted"  # отмена задачи оставит именно его
        try:
            try:
                if self.gate:
                    await self.gate.acquire()
                    self._owns_gate = True
                self.provider = await self._open_provider()
                self.loads += 1  # первая загрузка включает warm-up: два goto (N7)
                await self._send_progress(force=True)
                if self.settings.progress_edit_seconds > 0:
                    self._ticker = asyncio.create_task(self._tick())
                if self.kind == "sweep":
                    await self._sweep_all()
                elif self.kind == "deep":
                    await self._deep_scan()
                else:
                    await self._recheck_finds()
                    await self.discover_sections()
                    await self._crawl_all()
                status = "done"
            except StopRequested:
                status = "stopped"
            except BudgetExhausted:
                status = "budget"
            except ProviderBlocked:  # ждали человека и не дождались (или HEADLESS): сохраняем, продолжим по /report
                status = "blocked"
            except BreakerTripped:
                status = "failed"
            except Exception:
                log.exception("[MARKET] прогон упал")
                status = "failed"
        finally:
            if self._ticker:
                self._ticker.cancel()
                self._ticker = None
            if self.provider is not None:
                with contextlib.suppress(Exception):
                    await self.provider.__aexit__(None, None, None)
                self.provider = None
            if self._owns_gate:
                self.gate.release()
                self._owns_gate = False
            try:
                self._finish(status)
            except Exception:  # БД занята: итог всё равно отправим, статус поправит mark_interrupted при старте
                log.exception("[MARKET] статус прогона не записан")
            with contextlib.suppress(Exception):
                await self._send_progress(force=True, status=status)  # итоговый заголовок вместо ⏳
        try:
            await self._summary(status)
        except Exception:
            log.exception("[MARKET] итог не отправлен")

    def _finish(self, status: str) -> None:
        """Статус и run.loads пишутся на любом выходе, в том числе при отмене (N7)."""
        with self.session_factory() as db:
            run = db.get(CrawlRun, self.run_id)
            run.status, run.finished_at = status, self.clock()
            self._add_loads(run)
            db.commit()

    def _add_loads(self, run: CrawlRun) -> None:
        """run.loads копит загрузки всех нажатий /report, self.loads — только текущего."""
        run.loads += self.loads - self._loads_saved
        self._loads_saved = self.loads

    async def _crawl_all(self) -> None:
        with self.session_factory() as db:
            fb = dict(
                db.execute(select(Find.category_id, func.sum(Find.feedback)).group_by(Find.category_id)).all()
            )  # 👍/👎 из кнопок под порциями
            order = crawl_order(self._categories(db), self.run_id, self.clock(), fb)
        streak = 0
        for cat in order:
            if self.stop_requested:
                raise StopRequested
            if self.loads >= self.budget:
                raise BudgetExhausted
            # предохранитель (M4): Chromium упал / окно закрыли -> каждая следующая подкатегория падает мгновенно
            streak = 0 if await self.crawl_subcategory(cat.id) else streak + 1
            if streak >= MAX_ERROR_STREAK:
                log.error("[MARKET] %d ошибок подряд, прогон остановлен", streak)
                raise BreakerTripped
            await self._send_progress()

    async def _deep_scan(self) -> None:
        """Phase 4 (ADR-025): норма категорий → 1 карточка на кандидата → вторые карточки сильным. Только карточки,
        без профилей продавцов; объявление не открывается чаще раза в сутки; всё через AvitoTraffic."""
        from app.services import candidates as cand_engine
        from app.services import deep_scan as plan

        if self.settings.deep_start_delay_min > 0:
            await self._idle(self.settings.deep_start_delay_min, "⏳ старт через пару минут", None)
        now = self.clock()
        with self.session_factory() as db:
            cands = [c for c in cand_engine.run(db, now)["candidates"] if c.confidence in ("MEDIUM", "LOW")]
            base = plan.plan_baseline(db, now, cands, min(plan.BASELINE_BUDGET, self.budget - 1))
            planned = set(plan.categories_of(db, base))
        await self._observe_all(base)
        with self.session_factory() as db:
            first = plan.plan_first_pass(db, now, cands, planned_baseline=planned)
        await self._observe_all(first)
        for _ in range(2):  # вторая, затем третья карточка — пока есть бюджет
            left = self.budget - self.loads
            if left <= 0:
                break
            with self.session_factory() as db:
                more = plan.plan_followups(db, self.clock(), cands, left)
            if not more:
                break
            await self._observe_all(more)

    async def _observe_all(self, picks) -> None:
        for pick in picks:
            if self.loads >= self.budget:
                raise BudgetExhausted
            self._current = f"🔬 {pick.bucket}: {pick.url_path[-40:]}"
            await self._observe(pick)
            await self._send_progress()

    async def _observe(self, pick) -> None:
        """Одна карточка: просмотры, дата со страницы, ссылка продавца → card_obs (bucket выборки)."""
        now = self.clock()
        try:
            res = await self._fetch(BASE_URL + pick.url_path, SELECTORS["item_views"][0], "card")
        except (StopRequested, BudgetExhausted, ProviderBlocked, BrowserLost):
            raise
        except Exception as e:
            log.warning("[DEEP] карточка %s: %s", pick.url_path, e)
            self.errors.append(f"{pick.url_path[-50:]}: {type(e).__name__}: {e}")
            return
        with self.session_factory() as db:
            if is_gone(res.html, res.title, res.status):
                record_gone(db, pick.ad_id, now)
            else:
                st = parse_item_page(res.html)
                page_date = parse_published(st.date_text, now, absolute_time=True)
                record_card(db, pick.ad_id, now, self.run_id, pick.bucket, st.views, st.today, page_date, "card",
                            st.seller_url)  # fmt: skip
            db.commit()
        self.deep_counts[pick.bucket] += 1

    async def _sweep_all(self) -> None:
        """Ежедневный обход выдачи (ADR-016): глубина по темпу категории, тихие — через день, давно не бывшие —
        первыми; прерванная категория продолжается со следующей страницы (чекпойнт scan_categories)."""
        s, now = self.settings, self.clock()
        with self.session_factory() as db:
            cats = self._categories(db)
            scans = db.scalars(
                select(ScanCategory)
                .where(ScanCategory.category_id.in_([c.id for c in cats]))
                .order_by(ScanCategory.at.desc())
            ).all()
        mine = {r.category_id: r for r in scans if r.run_id == self.run_id}
        past: dict[int, list] = defaultdict(list)
        for r in scans:
            if r.done and r.run_id != self.run_id:
                past[r.category_id].append(r)
        plan, self._quiet = [], 0
        for c in cats:
            cur = mine.get(c.id)
            if cur is not None and cur.done:
                continue
            hist = past[c.id]
            rate = rate_per_hour(hist)
            since = (now - hist[0].at).total_seconds() / 3600 if hist else None
            if cur is None and not self._due(c.scope, since):
                continue  # не по графику своей области
            # тихие — через день только у подтверждённых CORE; гипотезы копят историю ежедневно (ADR-021)
            if cur is None and c.scope == "CORE" and c.scope_status == "confirmed" and is_quiet(rate, since, s):
                self._quiet += 1
                continue
            start = cur.pages + 1 if cur is not None else 1
            depth = plan_depth(rate, since, s) if c.scope == "CORE" else 1  # не CORE — только 1-я страница
            plan.append((math.inf if since is None else since, c, start, max(depth, start)))
        plan.sort(key=lambda p: -p[0])  # давно не были — первыми
        explore = [p for p in plan if p[1].scope == "EXPLORE"][s.explore_per_run :]
        plan = [p for p in plan if p not in explore]  # EXPLORE — не больше explore_per_run за обход
        streak = 0
        for _, cat, start, depth in plan:
            if self.stop_requested:
                raise StopRequested
            if self.loads >= self.budget:
                raise BudgetExhausted
            self._current = current_label(cat.section, cat.name)
            try:
                await self._collect(
                    cat.id, cat.url, cat.name, max_pages=depth, start_page=start, known_stop=True,
                    sample_total=s.sweep_large_total,
                )  # fmt: skip
                streak = 0
            except (StopRequested, BudgetExhausted, ProviderBlocked):
                self._save(None)
                raise
            except Exception as e:
                log.exception("[MARKET] обход %r упал", cat.name)
                self.errors.append(f"{cat.name}: {type(e).__name__}: {e}")
                streak += 1
                if streak >= MAX_ERROR_STREAK:
                    raise BreakerTripped from e
            await self._send_progress()

    def _due(self, scope: str, since: float | None) -> bool:
        """Пора ли обходить категорию области scope, если в последний раз были since часов назад."""
        s = self.settings
        every = {"WATCH": s.watch_every_hours, "QUERY": s.query_every_hours, "EXPLORE": s.explore_every_days * 24}
        return since is None or since >= every.get(scope, 0)

    def _categories(self, db, scopes: tuple[str, ...] | None = None) -> list[Category]:
        """Категории текущего вида прогона: обход — все активные области, /report — CORE/WATCH/QUERY (ADR-020)."""
        scopes = scopes or (ACTIVE if self.kind == "sweep" else REPORT_SCOPES)
        return list(
            db.scalars(
                select(Category).where(
                    Category.section.in_(split_csv(self.settings.report_sections)),
                    Category.skipped.is_not(True),
                    Category.scope.in_(scopes),
                )
            )
        )

    # --- сообщения ---

    def _progress_text(self, status: str | None = None) -> str:
        sweep = self.kind == "sweep"
        with self.session_factory() as db:
            cats = self._categories(db)
            if sweep:
                mine = db.scalars(select(ScanCategory).where(ScanCategory.run_id == self.run_id)).all()
                subcats = sum(r.done for r in mine)
                finds, hot = sum(r.new_ads for r in mine), 0
            else:
                subcats = len([c for c in cats if c.last_run_id == self.run_id])
                finds = db.get(CrawlRun, self.run_id).finds_count
                hot = db.scalar(
                    select(func.count()).select_from(Find).where(Find.run_id == self.run_id, Find.hot.is_(True))
                )
        elapsed = (self.clock() - self._started).total_seconds() if self._started else 0
        extra = {"title": "📡 Обход рынка", "found_label": "Новых объявлений"} if sweep else {}
        return format_progress(
            elapsed, self.loads, self.budget, subcats, finds, hot or 0, self._current, status,
            total=len(cats), errors=len(self.errors), premium=self.settings.premium_emoji, **extra,
        )

    async def _tick(self) -> None:
        """Живой таймер: правит сообщение каждые PROGRESS_EDIT_SECONDS, даже посреди долгой подкатегории."""
        while True:
            await asyncio.sleep(self.settings.progress_edit_seconds)
            with contextlib.suppress(Exception):
                await self._send_progress(force=True)

    async def _send_progress(self, force: bool = False, status: str | None = None) -> None:
        """Одно сообщение, правится не чаще PROGRESS_EDIT_SECONDS (AC-4.1), без правки, если текст не изменился."""
        now = self.clock()
        every = timedelta(seconds=self.settings.progress_edit_seconds)
        if not force and self._progress_at and now - self._progress_at < every:
            return
        text = self._progress_text(status)
        if text == self._progress_last:
            return
        self._progress_at, self._progress_last = now, text
        if self._progress_id is None:
            if self._progress_tried:  # первая отправка не удалась: не повторяем
                return
            self._progress_tried = True
            self._progress_id = await self.notifier.send_text(text)
            if self._progress_id is not None:
                with self.session_factory() as db:
                    db.get(CrawlRun, self.run_id).progress_msg_id = self._progress_id
                    db.commit()
        else:
            await self.notifier.edit_text(self._progress_id, text)

    async def _send_many(self, texts: list[str]) -> bool:
        ok = True
        for i, text in enumerate(texts):
            if i:
                await asyncio.sleep(self.send_delay)  # лимиты Telegram
            ok &= await self.notifier.send_text(text) is not None
        return ok

    def _entries(self, db, finds: list[Find]):
        """Карточки находок: «уже было» — находка той же подкатегории в другом прогоне; «модель N раз» — по прогону."""
        if not finds:
            return []
        keys, cat_ids = {f.group_key for f in finds}, {f.category_id for f in finds}
        seen: dict[str, datetime] = {}
        for key, first in db.execute(  # один запрос на все находки, без N+1
            select(Find.group_key, func.min(Find.created_at))
            .where(Find.group_key.in_(keys), Find.category_id.in_(cat_ids), Find.run_id != self.run_id)
            .group_by(Find.group_key, Find.category_id)
        ):
            seen[key] = min(first, seen.get(key, first))
        run_finds = list(db.scalars(select(Find).where(Find.run_id == self.run_id)))
        return entries_for(db, finds, self.settings, seen, run_finds)

    async def _send_hot(self, finds: list[Find]) -> None:
        """🔥 находки уходят сразу, по одной карточке с кнопками 👍/👎; остальные только в итоге (ADR-013)."""
        for f in (f for f in finds if f.hot):
            try:
                with self.session_factory() as db:
                    (entry,) = self._entries(db, [f])
                markup = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(text="👍", callback_data=f"fb:{f.id}:1"),
                            InlineKeyboardButton(text="👎", callback_data=f"fb:{f.id}:-1"),
                        ]
                    ]
                )
                if await self.notifier.send_text(entry.card, markup) is not None:
                    self._mark_sent([f.id])  # sent = карточка уже ушла отдельным сообщением
            except Exception:
                log.exception("[MARKET] 🔥 находка не отправлена сразу, будет в итоге")

    def _mark_sent(self, ids: list[int]) -> None:
        with self.session_factory() as db:
            db.execute(update(Find).where(Find.id.in_(ids)).values(sent=True))
            db.commit()

    async def _summary(self, status: str) -> None:
        """Итог (формат D): все находки прогона (и 🔥 тоже) по разделам + ушедшие при перепроверке (ADR-013)."""
        if self.kind == "sweep":  # итог обхода = утренний радар, телеметрия обхода — свёрнутым блоком (ADR-017)
            await self._send_many(self.radar_texts(tail=self._sweep_summary(status)))
            return
        if self.kind == "deep":
            counts = ", ".join(f"{k} {v}" for k, v in self.deep_counts.items()) or "0"
            note = STATUS_NOTES.get(status, "").replace("/report", "кнопке «🔬 Проверить кандидатов»")
            head = (f"<b>🔬 Выборочные карточки</b>\nОткрыто: {counts} · загрузок {self.loads}"
                    f"\n{self.traffic.summary()}" + (f"\n{note}" if note else ""))  # fmt: skip
            from app.services.hunter_report import hunter_texts

            with self.session_factory() as db:
                texts = hunter_texts(db, self.clock())
            await self._send_many([head, *texts])
            return
        s = self.settings
        with self.session_factory() as db:
            finds = dedupe_finds(db.scalars(select(Find).where(Find.run_id == self.run_id)))
            gone = list(db.scalars(select(Find).where(Find.id.in_(self.gone_ids)))) if self.gone_ids else []
            cats = self._categories(db)
            done = [c for c in cats if c.last_run_id == self.run_id]
            covered = [
                (c.name, c.last_days_covered)
                for c in done
                if c.last_status == "ok" and c.last_days_covered is not None
                if c.last_days_covered < s.report_max_age_days
            ]
            remaining = len(cats) - len(done)
            shown = dedupe_finds(finds + gone)
            pr = s.premium_emoji
            entries = self._entries(db, shown)
            stats = stats_lines(len(finds), sum(bool(f.hot) for f in finds), len(done), len(gone), pr)
            notes = [STATUS_NOTES[status]] if status in STATUS_NOTES else []
            crawled = [c for c in done if c.last_status == "ok" and c.last_fresh_count is not None]
            texts = format_results(
                f"<b>{icon('📊', pr)} Проверка рынка · {self.clock():%d.%m}</b>",
                stats,
                entries,
                model_groups(shown),
                notes,
                summary_tail(
                    covered, self.errors, remaining if status != "done" else 0, s.report_max_age_days,
                    s.report_max_pages, pr,
                ),
                market=market_lines(crawled, pr),
                premium=pr,
            )
        await self._send_many(texts)

    def radar_texts(self, tail: str | None = None) -> list[str]:
        """MARKET RADAR по истории из БД (без загрузок): /radar и итог ежедневного обхода."""
        with self.session_factory() as db:
            return build_radar(db, self.clock(), self._categories(db), tail, self.settings.premium_emoji)

    def _sweep_summary(self, status: str) -> str:
        fields = ("pages", "new_ads", "known_ads", "stop_reason", "window_hours")
        with self.session_factory() as db:
            names = {c.id: c.name for c in self._categories(db)}
            mine = db.scalars(select(ScanCategory).where(ScanCategory.run_id == self.run_id)).all()
            rows = [
                NS(name=names.get(r.category_id, "?"), **{k: getattr(r, k) for k in fields}) for r in mine if r.done
            ]
        note = STATUS_NOTES.get(status, "").replace("/report", "/sweep") or None
        return format_sweep_summary(
            f"{self.clock():%d.%m}", rows, len(names), self._quiet, self.loads, self.captcha_waits, self.errors,
            status, note, self.settings.premium_emoji, pauses=self.blocks, traffic=self.traffic.summary(),
        )  # fmt: skip

    # --- загрузки ---

    async def _before_load(self, kind: str = "search") -> None:
        """Единственная точка перед каждой загрузкой: стоп, бюджет прогона, уступка браузера, общий темп Avito
        (интервал, час, сутки, лимит блоков — ADR-019), счётчик (M2)."""
        if self.stop_requested:
            raise StopRequested
        if self.loads >= self.budget:
            raise BudgetExhausted
        if self.gate and self.gate.contended:
            await self._handover()
        try:
            ok = await self.traffic.acquire(kind, lambda: self.stop_requested)
        except TrafficLimit as e:
            if e.reason == "blocks":
                raise ProviderBlocked("Avito ограничивал доступ слишком часто сегодня — продолжу завтра") from e
            raise BudgetExhausted from e
        if not ok:
            raise StopRequested
        self.loads += 1

    async def _open_provider(self) -> AvitoProvider:
        """Запуск браузера; один повтор через паузу: профиль после close() иногда ещё занят (m10)."""
        for attempt in (1, 2):
            try:
                provider = self.provider_factory()
                await provider.__aenter__()
                return provider
            except Exception as e:
                if attempt == 2:
                    raise
                log.warning("[MARKET] браузер не открылся (%s), повтор через %ss", e, self.reopen_delay)
                await asyncio.sleep(self.reopen_delay)

    async def _handover(self) -> None:
        """Мониторингу нужен браузер: закрыть Chromium, отдать замок, взять снова, открыть новый браузер (M1).

        Состояние подкатегории в памяти, ничего не теряется. Новый браузер = новый warm-up = +1 загрузка.
        Если ошибка случится между release и acquire, _owns_gate=False и finally в _run не отпустит чужой замок.
        """
        old, self.provider = self.provider, None
        with contextlib.suppress(Exception):
            await old.__aexit__(None, None, None)
        self.gate.release()
        self._owns_gate = False
        await self.gate.acquire()
        self._owns_gate = True
        try:
            self.provider = await self._open_provider()
        except Exception as e:  # браузера нет: карточки не должны «успешно» пройти пустыми (ADR-007)
            raise BrowserLost(f"{type(e).__name__}: {e}") from e
        self.loads += 1

    async def _fetch(self, url: str, ready_selector: str | None = None, kind: str = "search"):
        """Блок: стоп всех загрузок; сначала человек (капча, ADR-005) → 5 мин тишины и замедленный темп; нет человека —
        пауза 60/120 мин и повтор (ADR-018); MAX_BLOCKS_PER_DAY блоков за сутки → ProviderBlocked, стоп до завтра."""
        await self._before_load(kind)
        pauses = 0
        while True:
            try:
                return await self._timed_fetch(url, ready_selector)
            except ProviderBlocked as e:
                if self.traffic.on_block():
                    raise ProviderBlocked(f"{e} — лимит блоков на сутки, продолжу завтра") from e
                if await self._wait_for_human(url, e):
                    minutes = self.traffic.on_human_passed() / 60
                    await self._idle(minutes, "⏳ после капчи — пауза", note=None)
                elif pauses >= self.settings.block_cooldowns:
                    raise
                else:
                    await self._cooldown(pauses)
                    pauses += 1
                    self.traffic.on_pause_done()
            await self._before_load(kind)

    async def _cooldown(self, n: int) -> None:
        """Пауза после блока без человека (ADR-018)."""
        minutes = self.settings.block_cooldown_minutes * 2**n
        self.blocks += 1
        until = self.clock() + timedelta(minutes=minutes)
        log.warning("[MARKET] блок без капчи: пауза %d мин", minutes)
        note = f"⏸ Avito ограничил доступ. Пауза <code>{minutes}</code> мин, продолжу сам в {until:%H:%M}"
        await self._idle(minutes, f"⏸ пауза после блока до {until:%H:%M}", note)

    async def _idle(self, minutes: float, label: str, note: str | None) -> None:
        """Ни одной загрузки minutes минут: браузер открыт, /stop работает. ponytail: замок браузера держится всю
        паузу, мониторинг ждёт — он выключен; отдавать замок, если мониторинг вернётся."""
        if note:
            await self.notifier.send_text(note)
        before, self._current = self._current, label
        await self._send_progress(force=True)
        loop = asyncio.get_running_loop()
        deadline = loop.time() + minutes * self.cooldown_unit
        try:
            while loop.time() < deadline:
                if self.stop_requested:
                    raise StopRequested
                await asyncio.sleep(max(min(self.cooldown_poll, deadline - loop.time()), 0))
        finally:
            self._current = before

    async def _timed_fetch(self, url: str, ready_selector: str | None):
        if self.provider is None:
            raise BrowserLost("браузер закрыт")
        async with asyncio.timeout(self.fetch_timeout):
            return await self.provider.fetch(url, ready_selector)

    async def _wait_for_human(self, url: str, reason: ProviderBlocked) -> bool:
        """ADR-005: капчу проходит человек в окне бота; бот её не решает. Браузер остаётся открытым на заблокированной
        странице. True: проверка пройдена, загрузку можно повторить. Нужен видимый браузер (HEADLESS=false)."""
        minutes = self.settings.captcha_wait_minutes
        log.error("[MARKET] блок: %s", reason)
        if self.settings.headless or minutes <= 0:
            return False
        self.captcha_waits += 1
        await self.notifier.send_text(captcha_alert(minutes, self.settings.premium_emoji))
        before, self._current = self._current, "🧩 жду проверку капчи"
        await self._send_progress(force=True)
        try:
            passed = await self.provider.wait_unblocked(
                url, minutes * 60, self.captcha_poll, lambda: self.stop_requested
            )
        finally:
            self._current = before
        if self.stop_requested:
            raise StopRequested
        if passed:
            await self.notifier.send_text(captcha_passed(self.settings.premium_emoji))
        return passed

    # --- перепроверка прошлых находок («ушло за N дней») ---

    async def _recheck_finds(self) -> None:
        """До обхода: открыть до RECHECK_MAX прошлых находок (2-14 дней, ещё не ушли). Страница снята/продана ->
        gone_at; жива -> last_checked_at и текущие просмотры. Блок/стоп/бюджет идут обычным путём (ADR-005)."""
        now, limit = self.clock(), self.settings.recheck_max
        if limit <= 0:
            return
        with self.session_factory() as db:
            rows = db.scalars(
                select(Find)
                .where(
                    Find.gone_at.is_(None),
                    Find.created_at <= now - RECHECK_MIN_AGE,
                    Find.created_at >= now - RECHECK_MAX_AGE,
                )
                .order_by(Find.last_checked_at.is_not(None), Find.last_checked_at, Find.created_at)
            ).all()
            todo: dict[str, tuple[int, str]] = {}
            for f in rows:  # одно объявление могло быть найдено в нескольких прогонах: открываем один раз
                todo.setdefault(f.external_id, (f.id, f.url))
        if todo:
            self._current = "перепроверка находок"
        for ext_id, (_, url) in list(todo.items())[:limit]:
            if self.loads >= self.budget:
                break  # перепроверка не съедает бюджет обхода целиком: на обход остаётся только то, что осталось
            try:
                res = await self._fetch(url, SELECTORS["item_views"][0], "recheck")
                gone = is_gone(res.html, res.title, res.status)
                st = None if gone else parse_item_page(res.html)
            except (StopRequested, BudgetExhausted, ProviderBlocked, BrowserLost):
                raise
            except Exception as e:
                log.warning("[MARKET] перепроверка %s: %s", url, e)
                continue
            self._save_recheck(ext_id, gone, st.views if st else None, st.today if st else None)

    def _save_recheck(self, ext_id: str, gone: bool, views: int | None, today: int | None = None) -> None:
        with self.session_factory() as db:
            now = self.clock()
            if gone:
                record_gone(db, ext_id, now)
            elif views is not None:
                record_card(db, ext_id, now, self.run_id, "recheck", views, today)
            marked = False
            for f in db.scalars(select(Find).where(Find.external_id == ext_id)):
                f.last_checked_at = now
                if gone and f.gone_at is None:
                    f.gone_at = now
                    if not marked:  # в итоге одна строка на объявление
                        self.gone_ids.append(f.id)
                        marked = True
                if views is not None:
                    f.views_last = views
            self._add_loads(db.get(CrawlRun, self.run_id))
            db.commit()

    # --- разделы ---

    async def discover_sections(self) -> None:
        """Подкатегории разделов со страницы раздела: upsert, перечитывание не чаще раза в 30 дней (AC-2.1)."""
        s, now = self.settings, self.clock()
        for section in split_csv(s.report_sections):
            with self.session_factory() as db:
                known = db.scalars(select(Category.discovered_at).where(Category.section == section)).all()
            if known and max(known) > now - SUBCAT_REFRESH:
                continue
            try:
                res = await self._fetch(f"{BASE_URL}/rossiya/{section}", SELECTORS["subcat"][0])
                subs = parse_subcategories(res.html, section, s.report_max_subcats)
            except (StopRequested, BudgetExhausted, ProviderBlocked, BrowserLost):
                raise
            except Exception as e:
                log.exception("[MARKET] раздел %s: не удалось прочитать подкатегории", section)
                self.errors.append(f"раздел {section}: {type(e).__name__}: {e}")
                continue
            if not subs:
                log.warning("[MARKET] раздел %s: подкатегорий не найдено (проверь SELECTORS)", section)
            with self.session_factory() as db:
                by_url = {c.url: c for c in db.scalars(select(Category).where(Category.section == section))}
                for name, url in subs:
                    cat = by_url.get(url) or Category(section=section, url=url, scope="OFF")  # нет в scope.py
                    cat.name, cat.discovered_at = name, now
                    db.add(cat)
                self._add_loads(db.get(CrawlRun, self.run_id))
                db.commit()

    # --- подкатегория ---

    async def crawl_subcategory(self, cat_id: int) -> bool:
        """True — обошли; False — ошибка (подкатегория пропущена, прогон идёт дальше).

        StopRequested / BudgetExhausted / ProviderBlocked пробрасываются; затраченные загрузки сохраняются.
        Находки пишутся одним коммитом в конце: прерванная подкатегория при продолжении проходится заново.
        """
        with self.session_factory() as db:
            cat = db.get(Category, cat_id)
            url, name = cat.url, cat.name
            self._current = current_label(cat.section, name)
        try:
            cards, covered = await self._collect(cat_id, url, name)
            finds, best_vpd, opened = await self._evaluate(cat_id, cards)
        except (StopRequested, BudgetExhausted, ProviderBlocked):
            self._save(None)
            raise
        except Exception as e:
            log.exception("[MARKET] подкатегория %r упала", name)
            self.errors.append(f"{name}: {type(e).__name__}: {e}")
            self._save(cat_id, error=True)
            return False
        added = self._save(
            cat_id, finds=finds, best_vpd=best_vpd, covered=covered, stats=market_stats(cards, opened)
        )
        await self._send_hot(added)
        return True

    async def _collect(
        self,
        cat_id: int,
        url: str,
        name: str,
        max_pages: int | None = None,
        start_page: int = 1,
        known_stop: bool = False,
        sample_total: int | None = None,
    ) -> tuple[list[Listing], float]:
        """Листает выдачу по дате; возвращает кандидатов и сколько дней из max_age покрыто.
        Каждая прочитанная страница сразу пишется в историю и чекпойнт (ADR-015/016): прерывание не теряет увиденное.
        known_stop (обход): стоп, когда доля уже знакомых непромо-объявлений страницы >= SWEEP_KNOWN_STOP."""
        s, now = self.settings, self.clock()
        limit = timedelta(days=s.report_max_age_days)
        found: dict[str, Listing] = {}
        last_age: timedelta | None = None
        full = False
        stop, total, pages, seen, new, known = "depth_cap", None, 0, 0, 0, 0
        with self.session_factory() as db:
            cat = db.get(Category, cat_id)
            is_query = cat.kind == "query"
            matches = query_matcher(cat.name) if is_query else None  # поиск Avito «протекает» (ADR-021)
        if start_page > 1:  # продолжение прерванной категории: счётчики уже прочитанных страниц
            with self.session_factory() as db:
                prev = db.get(ScanCategory, (self.run_id, cat_id))
                if prev is not None:
                    total, pages, seen = prev.total_count, prev.pages, prev.cards_seen
                    new, known = prev.new_ads, prev.known_ads
        for page in range(start_page, (max_pages or s.report_max_pages) + 1):
            sep = "&" if "?" in url else "?"  # подкатегория может быть поиском по слову: .../muzhskaya_odezhda?q=prada
            res = await self._fetch(with_page(f"{url}{sep}s=104&pmin={s.min_price}", page), SELECTORS["card"][0])
            pages += 1
            cards = parse_search_html(res.html, name, now)
            if page == 1:
                total = parse_total_count(res.html)
            promo = promoted_ids(res.html)
            with self.session_factory() as db:
                relevant = [c for c in cards if matches(c.title)] if matches else cards
                n, k = record_search(db, cat_id, relevant, promo, now, query=is_query)
                seen, new, known = seen + n + k, new + n, known + k
                db.merge(self._scan_row(cat_id, now, pages, seen, new, known, total, last_age, "partial", False))
                db.commit()
            if not cards:
                log.warning("[MARKET] 0 карточек: %s стр. %d", url, page)
                stop = "empty"
                full = not (page == 1 and total)  # пустая 1-я страница при «N объявлений» — сбой вёрстки, не «пусто»
                break
            old = False
            for c in cards:
                if c.external_id in promo or c.published_at is None:
                    continue  # промо и нераспознанная дата листание не останавливают
                age = now - c.published_at
                if age > limit:
                    old = True  # дочитываем страницу до конца (m1)
                    continue
                last_age = age
                if matches and not matches(c.title):
                    continue  # чужое объявление в выдаче запроса — не находка и не сигнал
                if c.price and c.price >= s.min_price:  # локальная перепроверка: pmin Avito может игнорировать
                    found.setdefault(c.external_id, c)
            if old:
                full, stop = True, "age_limit"
                break
            if sample_total and total and total >= sample_total:
                stop = "sample"  # огромная категория: 1-я страница = свежие модели + total + темп (ADR-019)
                break
            if known_stop and n + k and k / (n + k) >= s.sweep_known_stop:
                stop = "known"  # дальше — уже виденное в прошлых обходах
                break
        covered = float(s.report_max_age_days) if full else (last_age.total_seconds() / 86400 if last_age else 0.0)
        with self.session_factory() as db:
            db.merge(self._scan_row(cat_id, now, pages, seen, new, known, total, timedelta(days=covered), stop, True))
            db.commit()
        return list(found.values()), covered

    def _scan_row(self, cat_id, now, pages, seen, new, known, total, window: timedelta | None, stop, done):
        hours = window.total_seconds() / 3600 if window else 0.0
        return ScanCategory(
            run_id=self.run_id, category_id=cat_id, at=now, pages=pages, cards_seen=seen, new_ads=new,
            known_ads=known, total_count=total, window_hours=hours, stop_reason=stop, done=done,
        )  # fmt: skip

    async def _evaluate(self, cat_id: int, cards: list[Listing]) -> tuple[list[Find], int, list[Opened]]:
        s, now = self.settings, self.clock()
        finds: list[Find] = []
        all_opened: list[Opened] = []  # для среза рынка: все открытые страницы, в том числе ниже порога
        opened_pages = 0
        card_errors = 0
        for group, full in pick_groups(group_cards(cards)):
            opened: list[Opened] = []
            dropped = False
            for card in group:
                if opened_pages >= s.report_cards_per_subcat:
                    break
                opened_pages += 1
                res = await self._open_card(card)
                if res == "error":
                    card_errors += 1
                    continue
                if res == "old":  # дата на странице старше недели: группа отброшена, профиль не смотрим (AC-3.5)
                    dropped = True
                    break
                if res:
                    opened.append(res)
                    all_opened.append(res)
                    if res.vpd >= s.vpd_min:
                        break  # вторая копия открывается только если первая не дала порог (AC-2.4a)
            if dropped or not opened:
                continue
            best = max(opened, key=lambda o: o.vpd)
            if best.vpd < s.vpd_min:
                continue
            age, vpd = best.age, best.vpd
            seller_date = await self._seller_date(best) if s.check_seller_date and best.seller_url else None
            if seller_date:
                age = age_days(seller_date, now)
                vpd = calc_vpd(best.views, age)
            if not is_find(vpd, best.card.price, age, s):
                continue
            prices = [c.price for c in full]  # диапазон цен и копии — по всей группе, не по открытым (ADR-007)
            finds.append(
                Find(
                    category_id=cat_id,
                    group_key=norm_title(best.card.title),
                    title=best.card.title,
                    price_min=min(prices),
                    price_max=max(prices),
                    views=best.views,
                    vpd=vpd,
                    today=best.today,
                    page_date=best.page_date,
                    seller_date=seller_date,
                    age_days=age,
                    date_checked=date_checked(seller_date, s.check_seller_date),
                    copies=len(full),
                    url=best.card.url,
                    external_id=best.card.external_id,
                    hot=is_hot(vpd, s),
                    created_at=now,
                )
            )
        if opened_pages and card_errors == opened_pages:  # все открытые карточки упали: сбой, а не «пусто»
            raise RuntimeError(f"не открылась ни одна из {opened_pages} карточек")
        return finds, max((f.vpd for f in finds), default=0), all_opened

    async def _open_card(self, card: Listing) -> Opened | str | None:
        """Opened; 'old' — дата на странице старше недели; 'error' — не открылась; None — пропущена (нет счётчика)."""
        now, s = self.clock(), self.settings
        try:
            res = await self._fetch(card.url, SELECTORS["item_views"][0], "card")
            st = parse_item_page(res.html)
            if st.views is None:
                log.warning("[MARKET] нет счётчика просмотров: %s", card.url)
                return None
            page_date = parse_published(st.date_text, now, absolute_time=True)
            with self.session_factory() as db:
                record_card(db, card.external_id, now, self.run_id, "report", st.views, st.today, page_date, "card",
                            st.seller_url)  # fmt: skip
                db.commit()
            if page_date and now - page_date > timedelta(days=s.report_max_age_days):
                return "old"
            age = find_age(page_date, card.published_at, now)
            if age is None:
                return None
            return Opened(card, st.views, st.today, page_date, age, calc_vpd(st.views, age), st.seller_url)
        except (StopRequested, BudgetExhausted, ProviderBlocked, BrowserLost):
            raise
        except Exception as e:
            log.warning("[MARKET] карточка %s: %s", card.url, e)
            self.errors.append(f"{card.title[:60]}: {type(e).__name__}: {e}")
            return "error"

    async def _seller_date(self, o: Opened) -> datetime | None:
        """Дата объявления в профиле продавца; None — не удалось (карточка остаётся «дата не проверена», AC-3.4)."""
        try:
            res = await self._fetch(o.seller_url, SELECTORS["profile_item"][0], "profile")
            text = parse_seller_date(res.html, o.card.external_id)
            date = parse_published(text, self.clock(), absolute_time=True)
            if date:
                with self.session_factory() as db:
                    update_ad(db, o.card.external_id, date, "seller")
                    db.commit()
            return date
        except (StopRequested, BudgetExhausted, ProviderBlocked, BrowserLost):
            raise
        except Exception as e:
            log.warning("[MARKET] профиль продавца %s: %s", o.seller_url, e)
            return None

    def _save(
        self,
        cat_id: int | None,
        *,
        finds: list[Find] = (),
        error: bool = False,
        best_vpd: int = 0,
        covered: float | None = None,
        stats: dict | None = None,
    ) -> list[Find]:
        """Один коммит: находки + категория + run.loads (m8). cat_id=None — только счётчик загрузок.
        Возвращает реально добавленные находки (то же объявление в этом прогоне второй раз не пишем)."""
        added: list[Find] = []
        with self.session_factory() as db:
            run = db.get(CrawlRun, self.run_id)
            self._add_loads(run)
            if cat_id is not None:
                cat = db.get(Category, cat_id)
                cat.last_run_id = run.id
                if error:
                    cat.last_status = "error"  # last_crawled_at не трогаем: ротация перепроверит (M4)
                else:
                    cat.last_status, cat.last_crawled_at = "ok", self.clock()
                    cat.last_best_vpd, cat.last_days_covered = best_vpd, covered
                    have = set(db.scalars(select(Find.external_id).where(Find.run_id == run.id)))
                    for f in finds:
                        if f.external_id in have:
                            continue
                        have.add(f.external_id)
                        f.run_id = run.id
                        added.append(f)
                    db.add_all(added)
                    run.finds_count += len(added)
                    for key, value in (stats or {}).items():
                        setattr(cat, key, value)
            db.commit()
        return added
