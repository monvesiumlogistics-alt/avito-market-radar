import asyncio
import contextlib
import logging
import random
from datetime import datetime
from pathlib import Path

from playwright.async_api import BrowserContext, Playwright, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeout

from app.models import Listing, SearchUrl
from app.providers.avito_parser import BASE_URL, SELECTORS, is_blocked, parse_search_html, with_page
from app.providers.base import AvitoProvider, Page, ProviderBlocked

log = logging.getLogger(__name__)

BLOCK_STATUSES = {403, 429, 439}  # 439 — капча «проверка безопасности», 429 — «проблема с IP» (ADR-004)


def status_blocked(status: int | None) -> bool:
    return status in BLOCK_STATUSES


class AvitoBrowserProvider(AvitoProvider):
    """Обычный Chromium с постоянным профилем (cookies/логин из `python -m app.auth`). Без обходов защиты."""

    def __init__(
        self,
        profile_path: str,
        headless: bool,
        proxy: dict | None = None,
        debug_dir: str = "./data/debug",
        delay: tuple[float, float] = (0, 0),  # пауза перед загрузкой, с; у мониторинга нет, у проверки рынка 2–5
    ):
        self.profile_path = profile_path
        self.headless = headless
        self.proxy = proxy
        self.debug_dir = Path(debug_dir)
        self.delay = delay
        self._pw: Playwright | None = None
        self._ctx: BrowserContext | None = None
        self._warmed = False

    async def __aenter__(self) -> "AvitoBrowserProvider":
        self._pw = await async_playwright().start()
        try:
            self._ctx = await self._pw.chromium.launch_persistent_context(
                self.profile_path,
                headless=self.headless,
                proxy=self.proxy,
                locale="ru-RU",
                timezone_id="Europe/Moscow",
                viewport={"width": 1366, "height": 900},
            )
        except Exception:
            await self._pw.stop()
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        """Идемпотентен; после выхода экземпляр можно войти заново (свежий warm-up)."""
        ctx, pw, self._ctx, self._pw, self._warmed = self._ctx, self._pw, None, None, False
        if ctx:
            await ctx.close()
        if pw:
            await pw.stop()

    async def fetch(self, url: str, ready_selector: str | None = None) -> Page:
        assert self._ctx, "use `async with provider:`"
        tab = await self._ctx.new_page()
        try:
            if not self._warmed:
                # Сразу на выдачу Avito пускает хуже: сначала главная, как обычный человек.
                resp = await tab.goto(BASE_URL, wait_until="domcontentloaded", timeout=45_000)
                if status_blocked(resp and resp.status):
                    raise ProviderBlocked(f"Avito: HTTP {resp.status}")
                await tab.wait_for_timeout(2_000)
                self._warmed = True
            if self.delay[1]:
                await asyncio.sleep(random.uniform(*self.delay))
            resp = await tab.goto(url, wait_until="domcontentloaded", timeout=45_000)
            status = resp.status if resp else None
            if ready_selector:
                with contextlib.suppress(PlaywrightTimeout):  # пустая выдача или блок: разберёмся ниже
                    await tab.wait_for_selector(ready_selector, timeout=15_000)
            page = Page(await tab.content(), await tab.title(), tab.url)
        finally:
            await tab.close()

        if status_blocked(status) or is_blocked(page.html, page.title):
            self._dump(page.html, "blocked")
            raise ProviderBlocked(f"{page.title or 'Avito: доступ ограничен'} (HTTP {status}, {page.final_url})")
        return page

    async def wait_unblocked(self, url: str, timeout_s: float, poll_s: float = 5, cancel=None) -> bool:
        """ADR-005: человек проходит проверку в видимом окне, бот её не решает и не обходит.

        Открываем заблокированный url в отдельной вкладке и опрашиваем её, пока страница не перестанет быть блоком.
        """
        assert self._ctx, "use `async with provider:`"
        tab = await self._ctx.new_page()
        try:
            with contextlib.suppress(Exception):
                await tab.goto(url, wait_until="domcontentloaded", timeout=45_000)
            for _ in range(int(timeout_s // poll_s)):
                await asyncio.sleep(poll_s)
                if cancel and cancel():
                    return False
                with contextlib.suppress(Exception):
                    if not is_blocked(await tab.content(), await tab.title()):
                        return True
            return False
        finally:
            await tab.close()

    async def search(self, search: SearchUrl, page: int = 1) -> list[Listing]:
        fetched = await self.fetch(with_page(search.url, page), SELECTORS["card"][0])
        listings = parse_search_html(fetched.html, search.label)
        if not listings:
            self._dump(fetched.html)
        return listings

    def _dump(self, html: str, kind: str = "empty") -> None:
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        path = self.debug_dir / f"{kind}_{datetime.now():%Y%m%d_%H%M%S}.html"
        path.write_text(html, encoding="utf-8")
        log.warning("[FETCH] %s; HTML сохранён в %s", kind, path)
