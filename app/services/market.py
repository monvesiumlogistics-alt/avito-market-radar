"""Проверка рынка: обход подкатегорий Avito, поиск товаров с высоким спросом (просмотры в день)."""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import Category, CrawlRun, Find
from app.models import Listing
from app.providers.avito_parser import (
    SELECTORS,
    parse_item_page,
    parse_published,
    parse_search_html,
    parse_seller_date,
    promoted_ids,
    with_page,
)
from app.providers.base import AvitoProvider, ProviderBlocked
from app.services.market_logic import (
    age_days,
    calc_vpd,
    date_checked,
    find_age,
    group_cards,
    is_find,
    is_hot,
    norm_title,
    pick_cards,
)

log = logging.getLogger(__name__)

FETCH_TIMEOUT = 120  # с на одну загрузку: зависший Playwright не должен держать браузер (m9)


class MarketNotifier(Protocol):
    async def send_text(self, text: str) -> int | None: ...
    async def edit_text(self, message_id: int, text: str) -> None: ...


class StopRequested(Exception):
    """/stop: проверяется перед каждой загрузкой."""


class BudgetExhausted(Exception):
    """Бюджет загрузок прогона исчерпан."""


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

    # --- загрузки ---

    async def _before_load(self) -> None:
        """Единственная точка перед каждой загрузкой: стоп, бюджет, счётчик (M2). Здесь же будет уступка браузера."""
        if self.stop_requested:
            raise StopRequested
        if self.loads >= self.settings.report_budget:
            raise BudgetExhausted
        self.loads += 1

    async def _fetch(self, url: str, ready_selector: str | None = None):
        await self._before_load()
        async with asyncio.timeout(self.fetch_timeout):
            return await self.provider.fetch(url, ready_selector)

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
