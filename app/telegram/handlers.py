import html
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import KeyboardButton, Message, ReplyKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.db import ListingRow, WatchRule
from app.services.market import MarketCrawler
from app.services.notifier import NO_PREVIEW, format_price
from app.services.scanner import Scanner


def _hm(dt: datetime | None) -> str:
    return dt.strftime("%d.%m %H:%M") if dt else "—"


REPORT_BUTTON = "🔎 Проверить рынок"
STOP_BUTTON = "⏹ Стоп"
KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=REPORT_BUTTON), KeyboardButton(text=STOP_BUTTON)]], resize_keyboard=True
)


def build_router(
    admin_chat_id: int,
    scanner: Scanner,
    session_factory: sessionmaker,
    scheduler: AsyncIOScheduler,
    crawler: MarketCrawler,
) -> Router:
    router = Router()
    router.message.filter(F.chat.id == admin_chat_id)  # чужим бот молча не отвечает

    @router.message(Command("start"))
    async def start(msg: Message) -> None:
        await msg.answer(
            "<b>AvitoHunter</b>: мониторинг новых объявлений Avito.\n\n"
            "/status: состояние\n/watchlist: правила\n/check: проверить сейчас\n"
            "/last: последние найденные\n/pause, /resume: пауза мониторинга\n\n"
            "<b>Что выложить</b>: проверка рынка, час-полтора\n"
            "/report: запустить или продолжить, /stop: остановить",
            reply_markup=KEYBOARD,
        )

    @router.message(Command("report"))
    @router.message(F.text == REPORT_BUTTON)
    async def report(msg: Message) -> None:
        await msg.answer(crawler.start())

    @router.message(Command("stop"))
    @router.message(F.text == STOP_BUTTON)
    async def stop(msg: Message) -> None:
        await msg.answer(crawler.stop())

    @router.message(Command("status"))
    async def status(msg: Message) -> None:
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        job = scheduler.get_job("scan")
        lines = ["AvitoHunter работает ✅" if not scanner.blocked else "⚠️ Avito ограничил доступ", ""]
        with session_factory() as db:
            for rule in db.scalars(select(WatchRule)).all():
                found = db.scalar(select(func.count()).where(ListingRow.rule_id == rule.id, ListingRow.matched))
                new_today = db.scalar(
                    select(func.count()).where(
                        ListingRow.rule_id == rule.id,
                        ListingRow.notify_status.in_(("sent", "pending")),
                        ListingRow.first_seen_at >= today,
                    )
                )
                state = "DISABLED" if not rule.enabled else "PAUSED" if scanner.paused else "ACTIVE"
                lines += [
                    f"<b>{html.escape(rule.name)}</b>",
                    f"Статус: {state}",
                    f"Последняя проверка: {_hm(rule.last_checked_at)}",
                    f"Найдено объявлений: {found}",
                    f"Новых сегодня: {new_today}",
                    "",
                ]
                if not rule.search_urls:
                    lines.insert(-1, "⚠️ Нет AVITO_SEARCH_URLS в .env")
        if job and job.next_run_time and not scanner.paused:
            lines.append(f"Следующая проверка: ~{job.next_run_time:%H:%M}")
        await msg.answer("\n".join(lines))

    @router.message(Command("watchlist"))
    async def watchlist(msg: Message) -> None:
        lines = []
        with session_factory() as db:
            for r in db.scalars(select(WatchRule)).all():
                lines += [
                    f"<b>#{r.id} {html.escape(r.name)}</b> {'✅' if r.enabled else '⛔'}",
                    f"Цена: {r.price_min or 0}–{r.price_max or '∞'} ₽",
                    f"Ключи: {html.escape(', '.join(r.keywords))}",
                    f"Стоп-слова: {html.escape(', '.join(r.exclude_keywords)) or '—'}",
                    "Поиски: " + (", ".join(html.escape(u.get("label") or "без метки") for u in r.search_urls) or "—"),
                    "",
                ]
        await msg.answer("\n".join(lines) or "Правил нет")

    @router.message(Command("pause"))
    async def pause(msg: Message) -> None:
        scanner.paused = True
        await msg.answer("⏸ Мониторинг на паузе. /resume чтобы продолжить.")

    @router.message(Command("resume"))
    async def resume(msg: Message) -> None:
        scanner.paused = False
        await msg.answer("▶️ Мониторинг возобновлён.")

    @router.message(Command("check"))
    async def check(msg: Message) -> None:
        if scanner.busy:
            await msg.answer("Проверка уже идёт, подожди.")
            return
        await msg.answer("🔎 Проверяю...")
        report = await scanner.run_watch_rules(force=True)
        await msg.answer("Готово:\n" + "\n".join(html.escape(r) for r in report))

    @router.message(Command("last"))
    async def last(msg: Message) -> None:
        with session_factory() as db:
            rows = db.scalars(
                select(ListingRow).where(ListingRow.matched).order_by(ListingRow.first_seen_at.desc()).limit(10)
            ).all()
        if not rows:
            await msg.answer("Пока ничего не найдено.")
            return
        await msg.answer(
            "\n\n".join(
                f'<a href="{html.escape(r.url)}">{html.escape(r.title[:80])}</a>\n'
                f"{format_price(r.price)} · {html.escape(r.location or '')} · {_hm(r.first_seen_at)}"
                for r in rows
            ),
            link_preview_options=NO_PREVIEW,
        )

    return router
