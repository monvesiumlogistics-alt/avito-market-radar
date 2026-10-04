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
from app.providers.avito_parser import msk_now
from app.providers.base import BrowserGate
from app.providers.traffic import AvitoTraffic
from app.services.market import MarketCrawler
from app.services.notifier import TelegramNotifier
from app.services.panels import startup_text
from app.services.scanner import Scanner
from app.services.scoping import apply_scope
from app.telegram.handlers import build_router


def setup_logging(level: str) -> None:
    sys.stdout.reconfigure(encoding="utf-8", line_buffering=True)  # cp1252 по умолчанию; построчно: лог не теряется
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s", stream=sys.stdout)
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)  # не спамить каждым апдейтом


def build_scheduler(scanner: Scanner, crawler: MarketCrawler | None = None, daily_at: str = "") -> AsyncIOScheduler:
    """Мониторинг по интервалу; ежедневный обход выдачи в daily_at МСК + догоняющий запуск после старта бота
    (ПК мог быть выключен в 09:00). /report (карточки) — только по кнопке (ADR-016)."""
    scheduler = AsyncIOScheduler()
    if crawler is not None and daily_at:
        hour, minute = daily_at.split(":")
        scheduler.add_job(
            crawler.daily, "cron", hour=int(hour), minute=int(minute), timezone="Europe/Moscow", id="sweep",
            max_instances=1, coalesce=True, misfire_grace_time=6 * 3600,
        )  # fmt: skip
        scheduler.add_job(crawler.daily, "date", run_date=datetime.now() + timedelta(minutes=2), id="sweep_catchup")
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
    scope = apply_scope(session_factory, msk_now())  # область обхода из app/scope.py (ADR-020)
    log.info("[SCOPE] %s", scope["counts"])
    if not rule.search_urls:
        log.warning("AVITO_SEARCH_URLS пуст: искать негде, добавь URL в .env")

    client = None
    if settings.telegram_mtproxy:  # без VPN: Telegram через TgWsProxy по MTProto
        from app.telegram import mtproto

        client = mtproto.make_client(settings.telegram_api_id, settings.telegram_api_hash,
                                     settings.telegram_mtproxy, "./data/tg_bot")  # fmt: skip
        bot, mt_session = await mtproto.start(client, settings.telegram_bot_token)
        log.info("[TG] MTProto через %s", settings.telegram_mtproxy.rsplit(":", 1)[0])
    else:
        bot = Bot(
            settings.telegram_bot_token,
            session=AiohttpSession(proxy=settings.telegram_proxy) if settings.telegram_proxy else None,
            default=DefaultBotProperties(parse_mode="HTML"),
        )
    notifier = TelegramNotifier(bot, settings.telegram_admin_chat_id)
    proxy = playwright_proxy(settings.avito_proxy)
    gate = BrowserGate()  # один профиль браузера на мониторинг и проверку рынка
    traffic = AvitoTraffic(session_factory, settings, clock=msk_now)  # общий темп запросов к Avito (ADR-019)
    scanner = Scanner(
        session_factory,
        lambda: AvitoBrowserProvider(settings.avito_profile_path, settings.headless, proxy),
        notifier,
        interval_minutes=settings.check_interval_minutes,
        initial_scan_notify=settings.initial_scan_notify,
        max_pages=settings.max_pages,
        gate=gate,
        premium=settings.premium_emoji,
        traffic=traffic,
    )
    delay = (settings.page_delay_min, settings.page_delay_max)
    crawler = MarketCrawler(
        session_factory,
        lambda: AvitoBrowserProvider(settings.avito_profile_path, settings.headless, proxy, delay=delay),
        notifier,
        settings,
        gate=gate,
        traffic=traffic,
    )
    crawler.mark_interrupted()  # прогон, оборванный перезапуском бота, можно продолжить по /report

    scheduler = build_scheduler(scanner, crawler, settings.daily_sweep_at)
    # недоставленное без связи с Telegram (например, без VPN) — досылать каждые 5 мин
    scheduler.add_job(notifier.flush_outbox, "interval", minutes=5, id="outbox", max_instances=1, coalesce=True,
                      next_run_time=datetime.now() + timedelta(seconds=30))  # fmt: skip
    scheduler.start()

    dp = Dispatcher()
    dp.include_router(build_router(settings.telegram_admin_chat_id, scanner, session_factory, scheduler, crawler))
    log.info("AvitoHunter запущен, интервал %d мин", settings.check_interval_minutes)
    every = settings.check_interval_minutes
    await notifier.send_text(startup_text(rule.name, every, rule.enabled, settings.premium_emoji))
    try:
        if client is not None:
            mtproto.attach(client, mt_session, bot, dp)
            await client.run_until_disconnected()
        else:
            await dp.start_polling(bot)
    finally:
        scheduler.shutdown(wait=False)
        await bot.session.close()
        if client is not None:
            await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
