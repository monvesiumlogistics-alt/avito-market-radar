import asyncio
import logging
import sys
from datetime import datetime, timedelta

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from apscheduler.schedulers.asyncio import AsyncIOScheduler

from app.config import playwright_proxy, settings
from app.db import init_db, sync_default_rule
from app.providers.avito_browser import AvitoBrowserProvider
from app.providers.base import BrowserGate
from app.services.market import MarketCrawler
from app.services.notifier import TelegramNotifier
from app.services.scanner import Scanner
from app.telegram.handlers import build_router


def setup_logging(level: str) -> None:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)  # cp1252 по умолчанию; построчно: лог не теряется
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)  # не спамить каждым апдейтом


def build_scheduler(scanner: Scanner) -> AsyncIOScheduler:
    """Единственная задача по расписанию — мониторинг; проверка рынка только по кнопке (AC-1.3)."""
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        scanner.run_watch_rules,
        "interval",
        minutes=scanner.interval_minutes,
        id="scan",
        next_run_time=datetime.now() + timedelta(seconds=10),
        max_instances=1,
        coalesce=True,
    )
    return scheduler


async def main() -> None:
    setup_logging(settings.log_level)
    log = logging.getLogger("app")
    if not settings.telegram_bot_token or not settings.telegram_admin_chat_id:
        sys.exit("Заполни TELEGRAM_BOT_TOKEN и TELEGRAM_ADMIN_CHAT_ID в .env (см. README)")

    session_factory = init_db(settings.database_url)
    rule = sync_default_rule(session_factory, settings)
    if not rule.search_urls:
        log.warning("AVITO_SEARCH_URLS пуст: искать негде, добавь URL в .env")

    bot = Bot(
        settings.telegram_bot_token,
        session=AiohttpSession(proxy=settings.telegram_proxy) if settings.telegram_proxy else None,
        default=DefaultBotProperties(parse_mode="HTML"),
    )
    notifier = TelegramNotifier(bot, settings.telegram_admin_chat_id)
    proxy = playwright_proxy(settings.avito_proxy)
    gate = BrowserGate()  # один профиль браузера на мониторинг и проверку рынка
    scanner = Scanner(
        session_factory,
        lambda: AvitoBrowserProvider(settings.avito_profile_path, settings.headless, proxy),
        notifier,
        interval_minutes=settings.check_interval_minutes,
        initial_scan_notify=settings.initial_scan_notify,
        max_pages=settings.max_pages,
        gate=gate,
    )
    delay = (settings.page_delay_min, settings.page_delay_max)
    crawler = MarketCrawler(
        session_factory,
        lambda: AvitoBrowserProvider(settings.avito_profile_path, settings.headless, proxy, delay=delay),
        notifier,
        settings,
        gate=gate,
    )
    crawler.mark_interrupted()  # прогон, оборванный перезапуском бота, можно продолжить по /report

    scheduler = build_scheduler(scanner)
    scheduler.start()

    dp = Dispatcher()
    dp.include_router(build_router(settings.telegram_admin_chat_id, scanner, session_factory, scheduler, crawler))
    log.info("AvitoHunter запущен, интервал %d мин", settings.check_interval_minutes)
    await notifier.send_text(f"AvitoHunter запущен ✅ Проверка каждые {settings.check_interval_minutes} мин.")
    try:
        await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
