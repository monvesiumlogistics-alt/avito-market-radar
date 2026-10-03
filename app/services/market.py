"""Проверка рынка: обход подкатегорий Avito, поиск товаров с высоким спросом (просмотры в день)."""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import func, select, update
from sqlalchemy.orm import sessionmaker

from app.config import Settings, split_csv
from app.db import Category, CrawlRun, Find
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
    promoted_ids,
    with_page,
)
from app.providers.base import AvitoProvider, BrowserLost, ProviderBlocked
from app.services.market_cmds import entries_for
from app.services.market_logic import (
    age_days,
    calc_vpd,
    crawl_order,
    current_label,
    date_checked,
    find_age,
    format_progress,
    format_results,
    group_cards,
    is_find,
    is_hot,
    model_groups,
    norm_title,
    pick_groups,
    summary_tail,
)

log = logging.getLogger(__name__)

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

    # --- жизненный цикл ---

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def start(self) -> str:
        """/report: ответ пользователю; обход идёт в фоновой задаче."""
        if self.running:
            return "Проверка уже идёт\n" + self._progress_text()
        now, s = self.clock(), self.settings
        with self.session_factory() as db:
            last = db.scalars(select(CrawlRun).order_by(CrawlRun.id.desc()).limit(1)).first()
            resume = (
                last is not None
                and last.status in RESUMABLE
                and last.started_at > now - timedelta(hours=s.report_resume_hours)
            )
            if resume:
                run = last
                run.status, run.finished_at = "running", None
                done = db.scalar(select(func.count()).select_from(Category).where(Category.last_run_id == run.id))
                reply = f"Продолжаю проверку (пройдено подкатегорий: {done})"
            else:
                run = CrawlRun(started_at=now)
                db.add(run)
                reply = "Начинаю проверку рынка"
            db.commit()
            self.run_id, self.loads, self._loads_saved = run.id, 0, 0
        self.stop_requested, self.errors = False, []
        self._progress_at = self._progress_id = None
        self.gone_ids = []
        self._started, self._current, self._progress_last = now, None, ""
        self._progress_tried = False  # на продолжении новое сообщение о прогрессе (m6)
        self._task = asyncio.create_task(self._run())
        return reply

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
            if self.loads >= self.settings.report_budget:
                raise BudgetExhausted
            # предохранитель (M4): Chromium упал / окно закрыли -> каждая следующая подкатегория падает мгновенно
            streak = 0 if await self.crawl_subcategory(cat.id) else streak + 1
            if streak >= MAX_ERROR_STREAK:
                log.error("[MARKET] %d ошибок подряд, прогон остановлен", streak)
                raise BreakerTripped
            await self._send_progress()

    def _categories(self, db) -> list[Category]:
        return list(
            db.scalars(
                select(Category).where(
                    Category.section.in_(split_csv(self.settings.report_sections)), Category.skipped.is_not(True)
                )
            )
        )

    # --- сообщения ---

    def _progress_text(self, status: str | None = None) -> str:
        with self.session_factory() as db:
            cats = self._categories(db)
            subcats = len([c for c in cats if c.last_run_id == self.run_id])
            finds = db.get(CrawlRun, self.run_id).finds_count
            hot = db.scalar(
                select(func.count()).select_from(Find).where(Find.run_id == self.run_id, Find.hot.is_(True))
            )
        elapsed = (self.clock() - self._started).total_seconds() if self._started else 0
        return format_progress(
            elapsed, self.loads, self.settings.report_budget, subcats, finds, hot or 0, self._current, status,
            total=len(cats), errors=len(self.errors), premium=self.settings.premium_emoji,
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
        s = self.settings
        with self.session_factory() as db:
            finds = list(db.scalars(select(Find).where(Find.run_id == self.run_id)))
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
            shown = finds + [g for g in gone if g.id not in {f.id for f in finds}]
            entries = self._entries(db, shown)
            stats = f"🎯 {len(finds)} находок · 🔥 {sum(bool(f.hot) for f in finds)} · 📂 {len(done)} подкатегорий"
            notes = [n for n in (STATUS_NOTES.get(status), f"✅ ушло: {len(gone)}" if gone else None) if n]
            texts = format_results(
                f"<b>📊 Проверка рынка · {self.clock():%d.%m}</b>",
                stats,
                entries,
                model_groups(shown),
                notes,
                summary_tail(covered, self.errors, remaining if status != "done" else 0, s.report_max_age_days),
            )
        await self._send_many(texts)

    # --- загрузки ---

    async def _before_load(self) -> None:
        """Единственная точка перед каждой загрузкой: стоп, бюджет, уступка браузера, счётчик (M2)."""
        if self.stop_requested:
            raise StopRequested
        if self.loads >= self.settings.report_budget:
            raise BudgetExhausted
        if self.gate and self.gate.contended:
            await self._handover()
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

    async def _fetch(self, url: str, ready_selector: str | None = None):
        await self._before_load()
        try:
            return await self._timed_fetch(url, ready_selector)
        except ProviderBlocked as e:
            if not await self._wait_for_human(url, e):
                raise
            await self._before_load()
            return await self._timed_fetch(url, ready_selector)

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
        await self.notifier.send_text(
            f"🧩 Avito просит проверку — пройди её в окне браузера бота (жду до {minutes} мин)"
        )
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
            await self.notifier.send_text("✅ Проверка пройдена, продолжаю")
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
            if self.loads >= self.settings.report_budget:
                break  # перепроверка не съедает бюджет обхода целиком: на обход остаётся только то, что осталось
            try:
                res = await self._fetch(url, SELECTORS["item_views"][0])
                gone = is_gone(res.html, res.title, res.status)
                views = None if gone else parse_item_page(res.html).views
            except (StopRequested, BudgetExhausted, ProviderBlocked, BrowserLost):
                raise
            except Exception as e:
                log.warning("[MARKET] перепроверка %s: %s", url, e)
                continue
            self._save_recheck(ext_id, gone, views)

    def _save_recheck(self, ext_id: str, gone: bool, views: int | None) -> None:
        with self.session_factory() as db:
            now = self.clock()
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
                    cat = by_url.get(url) or Category(section=section, url=url)
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
            cards, covered = await self._collect(url, name)
            finds, best_vpd = await self._evaluate(cat_id, cards)
        except (StopRequested, BudgetExhausted, ProviderBlocked):
            self._save(None)
            raise
        except Exception as e:
            log.exception("[MARKET] подкатегория %r упала", name)
            self.errors.append(f"{name}: {type(e).__name__}: {e}")
            self._save(cat_id, error=True)
            return False
        self._save(cat_id, finds=finds, best_vpd=best_vpd, covered=covered)
        await self._send_hot(finds)
        return True

    async def _collect(self, url: str, name: str) -> tuple[list[Listing], float]:
        """Листает выдачу по дате; возвращает кандидатов и сколько дней из max_age покрыто."""
        s, now = self.settings, self.clock()
        limit = timedelta(days=s.report_max_age_days)
        found: dict[str, Listing] = {}
        last_age: timedelta | None = None
        full = False
        for page in range(1, s.report_max_pages + 1):
            sep = "&" if "?" in url else "?"  # подкатегория может быть поиском по слову: .../muzhskaya_odezhda?q=prada
            res = await self._fetch(with_page(f"{url}{sep}s=104&pmin={s.min_price}", page), SELECTORS["card"][0])
            cards = parse_search_html(res.html, name, now)
            if not cards:
                log.warning("[MARKET] 0 карточек: %s стр. %d", url, page)
                full = True
                break
            promo, old = promoted_ids(res.html), False
            for c in cards:
                if c.external_id in promo or c.published_at is None:
                    continue  # промо и нераспознанная дата листание не останавливают
                age = now - c.published_at
                if age > limit:
                    old = True  # дочитываем страницу до конца (m1)
                    continue
                last_age = age
                if c.price and c.price >= s.min_price:  # локальная перепроверка: pmin Avito может игнорировать
                    found.setdefault(c.external_id, c)
            if old:
                full = True
                break
        covered = float(s.report_max_age_days) if full else (last_age.total_seconds() / 86400 if last_age else 0.0)
        return list(found.values()), covered

    async def _evaluate(self, cat_id: int, cards: list[Listing]) -> tuple[list[Find], int]:
        s, now = self.settings, self.clock()
        finds: list[Find] = []
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
        return finds, max((f.vpd for f in finds), default=0)

    async def _open_card(self, card: Listing) -> Opened | str | None:
        """Opened; 'old' — дата на странице старше недели; 'error' — не открылась; None — пропущена (нет счётчика)."""
        now, s = self.clock(), self.settings
        try:
            res = await self._fetch(card.url, SELECTORS["item_views"][0])
            st = parse_item_page(res.html)
            if st.views is None:
                log.warning("[MARKET] нет счётчика просмотров: %s", card.url)
                return None
            page_date = parse_published(st.date_text, now, absolute_time=True)
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
            res = await self._fetch(o.seller_url, SELECTORS["profile_item"][0])
            text = parse_seller_date(res.html, o.card.external_id)
            return parse_published(text, self.clock(), absolute_time=True)
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
    ) -> None:
        """Один коммит: находки + категория + run.loads (m8). cat_id=None — только счётчик загрузок."""
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
                    for f in finds:
                        f.run_id = run.id
                    db.add_all(finds)
                    run.finds_count += len(finds)
            db.commit()
