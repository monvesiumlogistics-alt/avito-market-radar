"""Один прогон проверки рынка без Telegram-кнопки (как /report). Профиль браузера должен быть свободен.

    PYTHONIOENCODING=utf-8 .venv/Scripts/python -m scripts.run_report
"""

import asyncio

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession

from app.config import playwright_proxy, settings
from app.db import init_db
from app.main import setup_logging
from app.providers.avito_browser import AvitoBrowserProvider
from app.services.market import MarketCrawler
from app.services.notifier import TelegramNotifier


async def main() -> None:
    setup_logging(settings.log_level)
    bot = Bot(
        settings.telegram_bot_token,
        session=AiohttpSession(proxy=settings.telegram_proxy) if settings.telegram_proxy else None,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    notifier = TelegramNotifier(bot, settings.telegram_admin_chat_id)
    proxy = playwright_proxy(settings.avito_proxy)
    delay = (settings.page_delay_min, settings.page_delay_max)
    crawler = MarketCrawler(
        init_db(settings.database_url),
        lambda: AvitoBrowserProvider(settings.avito_profile_path, settings.headless, proxy, delay=delay),
        notifier,
        settings,
    )
    crawler.mark_interrupted()
    await notifier.send_text("🔎 Прогон запущен вручную: " + crawler.start())
    try:
        while crawler.running:
            await asyncio.sleep(5)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
