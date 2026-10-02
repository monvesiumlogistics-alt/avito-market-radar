import contextlib
import logging
from datetime import datetime
from pathlib import Path

from playwright.async_api import BrowserContext, Playwright, async_playwright
from playwright.async_api import TimeoutError as PlaywrightTimeout

from app.models import Listing, SearchUrl
from app.providers.avito_parser import BASE_URL, SELECTORS, is_blocked, parse_search_html, with_page
from app.providers.base import AvitoProvider, ProviderBlocked

log = logging.getLogger(__name__)


class AvitoBrowserProvider(AvitoProvider):
    """Обычный Chromium с постоянным профилем (cookies/логин из `python -m app.auth`). Без обходов защиты."""

    def __init__(self, profile_path: str, headless: bool, proxy: dict | None = None, debug_dir: str = "./data/debug"):
        self.profile_path = profile_path
        self.headless = headless
        self.proxy = proxy
        self.debug_dir = Path(debug_dir)
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
        if self._ctx:
            await self._ctx.close()
        if self._pw:
            await self._pw.stop()

    async def search(self, search: SearchUrl, page: int = 1) -> list[Listing]:
        assert self._ctx, "use `async with provider:`"
        tab = await self._ctx.new_page()
        try:
            if not self._warmed:
                # Сразу на выдачу Avito пускает хуже: сначала главная, как обычный человек.
                await tab.goto(BASE_URL, wait_until="domcontentloaded", timeout=45_000)
                await tab.wait_for_timeout(2_000)
                self._warmed = True
            await tab.goto(with_page(search.url, page), wait_until="domcontentloaded", timeout=45_000)
            with contextlib.suppress(PlaywrightTimeout):  # пустая выдача или блок: разберёмся ниже
                await tab.wait_for_selector(SELECTORS["card"][0], timeout=15_000)
            html, title = await tab.content(), await tab.title()
        finally:
            await tab.close()

        if is_blocked(html, title):
            raise ProviderBlocked(title or "Avito: доступ ограничен")
        listings = parse_search_html(html, search.label)
        if not listings:
            self._dump(html)
        return listings

    def _dump(self, html: str) -> None:
        self.debug_dir.mkdir(parents=True, exist_ok=True)
        path = self.debug_dir / f"empty_{datetime.now():%Y%m%d_%H%M%S}.html"
        path.write_text(html, encoding="utf-8")
        log.warning("[FETCH] 0 карточек; HTML сохранён в %s (проверь SELECTORS)", path)
