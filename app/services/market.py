"""Проверка рынка: обход подкатегорий Avito, поиск товаров с высоким спросом (просмотры в день)."""

import asyncio
import contextlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import func, select, update
from sqlalchemy.orm import sessionmaker

from app.config import Settings, split_csv
from app.db import Category, CrawlRun, Find
from app.models import Listing
from app.providers.avito_parser import (
    BASE_URL,
    SELECTORS,
    parse_item_page,
    parse_published,
    parse_search_html,
    parse_seller_date,
    parse_subcategories,
    promoted_ids,
    with_page,
)
from app.providers.base import AvitoProvider, ProviderBlocked
from app.services.market_logic import (
    age_days,
    calc_vpd,
    crawl_order,
    date_checked,
    find_age,
    format_find,
    format_progress,
    format_summary,
    group_cards,
    is_find,
    is_hot,
    norm_title,
    pick_cards,
    sort_finds,
    split_message,
)

log = logging.getLogger(__name__)

RESUMABLE = ("running", "stopped", "blocked", "interrupted", "failed")  # budget/done: следующий прогон новый
PROGRESS_EVERY = timedelta(minutes=1)
MAX_ERROR_STREAK = 3
STATUS_NOTES = {
    "stopped": "Проверка остановлена, продолжу по /report.",
    "blocked": "⚠️ Avito ограничил доступ, продолжу по /report.",
    "failed": "⚠️ Проверка упала, найденное сохранено.",
    "budget": "Бюджет загрузок исчерпан.",
}

SUBCAT_REFRESH = timedelta(days=30)  # подкатегории раздела перечитываем не чаще
FETCH_TIMEOUT = 120  # с на одну загрузку: зависший Playwright не должен держать браузер (m9)


class MarketNotifier(Protocol):
    async def send_text(self, text: str) -> int | None: ...
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
        clock: Callable[[], datetime] = datetime.now,
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
        self.loads = 0
        self.errors: list[str] = []
        self.send_delay = 1.0  # пауза между кусками длинного сообщения (лимиты Telegram); в тестах 0
        self.captcha_poll = 5.0  # с между проверками, прошёл ли человек капчу
        self.reopen_delay = 2.0  # с до повторного запуска браузера (m10)
        self._task: asyncio.Task | None = None
        self._owns_gate = False
        self._progress_at: datetime | None = None
        self._progress_id: int | None = None

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
            self.run_id, self.loads = run.id, run.loads
        self.stop_requested, self.errors = False, []
        self._progress_at = self._progress_id = None  # на продолжении новое сообщение о прогрессе (m6)
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
            if self.provider is not None:
                with contextlib.suppress(Exception):
                    await self.provider.__aexit__(None, None, None)
                self.provider = None
            if self._owns_gate:
                self.gate.release()
                self._owns_gate = False
            self._finish(status)
        try:
            await self._summary(status)
        except Exception:
            log.exception("[MARKET] итог не отправлен")

    def _finish(self, status: str) -> None:
        """Статус и run.loads пишутся на любом выходе, в том числе при отмене (N7)."""
        with self.session_factory() as db:
            run = db.get(CrawlRun, self.run_id)
            run.status, run.finished_at, run.loads = status, self.clock(), self.loads
            db.commit()

    async def _crawl_all(self) -> None:
        with self.session_factory() as db:
            order = crawl_order(self._categories(db), self.run_id, self.clock())
        streak = 0
        for i, cat in enumerate(order):
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
            if i + 1 == len(order) or order[i + 1].section != cat.section:
                await self._portion(cat.section)

    def _categories(self, db) -> list[Category]:
        return list(db.scalars(select(Category).where(Category.section.in_(split_csv(self.settings.report_sections)))))

    # --- сообщения ---

    def _progress_text(self) -> str:
        with self.session_factory() as db:
            cats = self._categories(db)
            finds = db.get(CrawlRun, self.run_id).finds_count
        sections: dict[str, list[bool]] = {}
        for c in cats:
            sections.setdefault(c.section, []).append(c.last_run_id == self.run_id)
        done = sum(all(v) for v in sections.values())
        subcats = sum(c.last_run_id == self.run_id for c in cats)
        return format_progress(done, len(sections), subcats, self.loads, self.settings.report_budget, finds)

    async def _send_progress(self, force: bool = False) -> None:
        """Одно сообщение, правится не чаще раза в минуту (AC-4.1)."""
        now = self.clock()
        if not force and self._progress_at and now - self._progress_at < PROGRESS_EVERY:
            return
        self._progress_at = now
        text = self._progress_text()
        if self._progress_id is None:
            self._progress_id = await self.notifier.send_text(text)
            with self.session_factory() as db:
                db.get(CrawlRun, self.run_id).progress_msg_id = self._progress_id
                db.commit()
        else:
            await self.notifier.edit_text(self._progress_id, text)

    async def _send(self, text: str) -> bool:
        ok = True
        for i, chunk in enumerate(split_message(text)):
            if i:
                await asyncio.sleep(self.send_delay)
            ok &= await self.notifier.send_text(chunk) is not None
        return ok

    def _lines(self, db, finds: list[Find]) -> list[str]:
        """Строки находок: 🔥 первыми, «уже было» (находка в другом прогоне той же подкатегории) после новых."""
        seen: dict[str, datetime] = {}
        for f in finds:
            first = db.scalar(
                select(func.min(Find.created_at)).where(
                    Find.group_key == f.group_key, Find.category_id == f.category_id, Find.run_id != f.run_id
                )
            )
            if first:
                seen[f.group_key] = first
        return [
            format_find(f, db.get(Category, f.category_id).name, seen.get(f.group_key)) for f in sort_finds(finds, seen)
        ]

    async def _portion(self, section: str) -> None:
        """Находки только что пройденного раздела (AC-4.2)."""
        with self.session_factory() as db:
            finds = list(
                db.scalars(
                    select(Find)
                    .join(Category, Find.category_id == Category.id)
                    .where(Find.run_id == self.run_id, Find.sent.is_(False), Category.section == section)
                )
            )
            if not finds:
                return
            text = "\n".join([f"<b>{section.replace('_', ' ')} — находки</b>", *self._lines(db, finds)])
            ids = [f.id for f in finds]
        if await self._send(text):  # Telegram не принял — sent=0, находки всё равно попадут в итог
            self._mark_sent(ids)

    def _mark_sent(self, ids: list[int]) -> None:
        with self.session_factory() as db:
            db.execute(update(Find).where(Find.id.in_(ids)).values(sent=True))
            db.commit()

    async def _summary(self, status: str) -> None:
        s = self.settings
        with self.session_factory() as db:
            finds = list(db.scalars(select(Find).where(Find.run_id == self.run_id)))
            cats = self._categories(db)
            done = [c for c in cats if c.last_run_id == self.run_id]
            covered = [
                (c.name, c.last_days_covered)
                for c in done
                if c.last_status == "ok" and c.last_days_covered is not None
                if c.last_days_covered < s.report_max_age_days
            ]
            remaining = len(cats) - len(done)
            text = format_summary(
                self.clock(),
                self._lines(db, finds),
                covered,
                self.errors,
                remaining if status != "done" else 0,
                s.report_max_age_days,
                STATUS_NOTES.get(status),
            )
            ids = [f.id for f in finds]
        if await self._send(text):
            self._mark_sent(ids)

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
        self.provider = await self._open_provider()
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
        passed = await self.provider.wait_unblocked(url, minutes * 60, self.captcha_poll, lambda: self.stop_requested)
        if self.stop_requested:
            raise StopRequested
        if passed:
            await self.notifier.send_text("✅ Проверка пройдена, продолжаю")
        return passed

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
            except (StopRequested, BudgetExhausted, ProviderBlocked):
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
                run = db.get(CrawlRun, self.run_id)
                run.loads = self.loads
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
        return True

    async def _collect(self, url: str, name: str) -> tuple[list[Listing], float]:
        """Листает выдачу по дате; возвращает кандидатов и сколько дней из max_age покрыто."""
        s, now = self.settings, self.clock()
        limit = timedelta(days=s.report_max_age_days)
        found: dict[str, Listing] = {}
        last_age: timedelta | None = None
        full = False
        for page in range(1, s.report_max_pages + 1):
            res = await self._fetch(with_page(f"{url}?s=104&pmin={s.min_price}", page), SELECTORS["card"][0])
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
        for group in pick_cards(group_cards(cards)):
            opened: list[Opened] = []
            dropped = False
            for card in group:
                if opened_pages >= s.report_cards_per_subcat:
                    break
                opened_pages += 1
                res = await self._open_card(card)
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
            prices = [c.price for c in group]
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
                    copies=len(group),
                    url=best.card.url,
                    external_id=best.card.external_id,
                    hot=is_hot(vpd, s),
                    created_at=now,
                )
            )
        return finds, max((f.vpd for f in finds), default=0)

    async def _open_card(self, card: Listing) -> Opened | str | None:
        """Opened; 'old' — дата на странице старше недели; None — карточка пропущена (ошибка / нет счётчика)."""
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
        except (StopRequested, BudgetExhausted, ProviderBlocked):
            raise
        except Exception as e:
            log.warning("[MARKET] карточка %s: %s", card.url, e)
            self.errors.append(f"{card.title[:60]}: {type(e).__name__}: {e}")
            return None

    async def _seller_date(self, o: Opened) -> datetime | None:
        """Дата объявления в профиле продавца; None — не удалось (карточка остаётся «дата не проверена», AC-3.4)."""
        try:
            res = await self._fetch(o.seller_url, SELECTORS["profile_item"][0])
            text = parse_seller_date(res.html, o.card.external_id)
            return parse_published(text, self.clock(), absolute_time=True)
        except (StopRequested, BudgetExhausted, ProviderBlocked):
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
            run.loads = self.loads
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
