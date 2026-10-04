"""Один выборочный прогон карточек Phase 4 (ADR-025). Профиль браузера должен быть свободен (бот остановлен).

    PYTHONIOENCODING=utf-8 .venv/Scripts/python -m scripts.deep_scan --plan   # только план, 0 загрузок
    PYTHONIOENCODING=utf-8 .venv/Scripts/python -m scripts.deep_scan          # реальный прогон (DEEP_BUDGET)

Все загрузки — через общий AvitoTraffic (темп, суточный бюджет, лимит блоков); капча — человеком (ADR-005).
"""

import asyncio
import sys
from collections import Counter

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession

from app.config import playwright_proxy, settings
from app.db import Category, init_db
from app.providers.avito_parser import msk_now


def show_plan() -> None:
    from app.services import candidates as engine
    from app.services import deep_scan as plan

    now = msk_now()
    with init_db(settings.database_url)() as db:
        cands = engine.run(db, now)["candidates"]
        base = plan.plan_baseline(db, now, cands, min(plan.BASELINE_BUDGET, settings.deep_budget - 1))
        first = plan.plan_first_pass(db, now, cands, planned_baseline=set(plan.categories_of(db, base)))
        names = {c.metrics.product_id: c.metrics.name for c in cands}
        print(f"кандидатов {len(cands)} · бюджет {settings.deep_budget} (вкл. warm-up)")
        print("норма категорий:", {db.get(Category, k).name: v for k, v in plan.categories_of(db, base).items()})
        print(f"первый проход: {len(first)} товаров:", dict(Counter(names[p.product_id].split()[0] for p in first)))
        for p in first:
            print("  ", names[p.product_id])
        print(f"итого до вторых карточек: {1 + len(base) + len(first)} загрузок")


async def run() -> None:
    from app.main import setup_logging  # тянет браузер — только для реального прогона, не для --plan
    from app.providers.avito_browser import AvitoBrowserProvider
    from app.services.market import MarketCrawler
    from app.services.notifier import TelegramNotifier

    setup_logging(settings.log_level)
    bot = Bot(settings.telegram_bot_token,
              session=AiohttpSession(proxy=settings.telegram_proxy) if settings.telegram_proxy else None,
              default=DefaultBotProperties(parse_mode="HTML"))  # fmt: skip
    notifier = TelegramNotifier(bot, settings.telegram_admin_chat_id)
    proxy = playwright_proxy(settings.avito_proxy)
    delay = (settings.page_delay_min, settings.page_delay_max)
    crawler = MarketCrawler(
        init_db(settings.database_url),
        lambda: AvitoBrowserProvider(settings.avito_profile_path, settings.headless, proxy, delay=delay),
        notifier, settings,
    )  # fmt: skip
    crawler.mark_interrupted()
    await notifier.send_text("🔬 Выборочные карточки (Phase 4), вручную: " + crawler.start("deep"))
    try:
        while crawler.running:
            await asyncio.sleep(5)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    if "--plan" in sys.argv:
        show_plan()
    else:
        asyncio.run(run())
