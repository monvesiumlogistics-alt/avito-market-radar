import html
from datetime import datetime

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import BufferedInputFile, CallbackQuery, KeyboardButton, Message, ReplyKeyboardMarkup
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db import ListingRow, WatchRule
from app.providers.avito_parser import msk_now
from app.services.market import MarketCrawler
from app.services.market_cmds import cats_text, export_csv, set_feedback, set_price, set_skipped, top_messages
from app.services.notifier import NO_PREVIEW, format_price
from app.services.panels import start_text, status_header, status_next, status_rule
from app.services.scanner import Scanner


def _hm(dt: datetime | None) -> str:
    return dt.strftime("%d.%m %H:%M") if dt else "—"


REPORT_BUTTON = "🔎 Проверить рынок"
STOP_BUTTON = "⏹ Стоп"
DEEP_BUTTON = "🔬 Проверить кандидатов"
KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text=REPORT_BUTTON), KeyboardButton(text=DEEP_BUTTON)],
        [KeyboardButton(text=STOP_BUTTON)],
    ],
    resize_keyboard=True,
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
    router.callback_query.filter(F.message.chat.id == admin_chat_id)  # и на кнопки чужих тоже

    @router.message(Command("start"))
    async def start(msg: Message) -> None:
        await msg.answer(
            start_text(settings.premium_emoji),
            reply_markup=KEYBOARD,
        )

    @router.message(Command("report"))
    @router.message(F.text == REPORT_BUTTON)
    async def report(msg: Message) -> None:
        await msg.answer(crawler.start())

    @router.message(Command("deep"))
    @router.message(F.text == DEEP_BUTTON)
    async def deep(msg: Message) -> None:
        """Phase 4 по кнопке: выборочные карточки кандидатов (DEEP_BUDGET загрузок), итог — в конце прогона."""
        await msg.answer(crawler.start("deep"))

    @router.message(Command("sweep"))
    async def sweep(msg: Message) -> None:
        await msg.answer(crawler.start("sweep"))

    @router.message(Command("radar"))
    async def radar(msg: Message) -> None:
        for text in crawler.radar_texts():
            await msg.answer(text, link_preview_options=NO_PREVIEW)

    @router.message(Command("stop"))
    @router.message(F.text == STOP_BUTTON)
    async def stop(msg: Message) -> None:
        await msg.answer(crawler.stop())

    @router.message(Command("top"))
    async def top(msg: Message, command: CommandObject) -> None:
        arg = (command.args or "").strip()
        days = int(arg) if arg.isdigit() and 0 < int(arg) <= 365 else 7
        with session_factory() as db:
            chunks = top_messages(db, days, msk_now(), settings)
        for chunk in chunks:
            await msg.answer(chunk, link_preview_options=NO_PREVIEW)

    @router.message(Command("export"))
    async def export(msg: Message) -> None:
        with session_factory() as db:
            data = export_csv(db)
        if data.count(b"\n") <= 1:
            await msg.answer("Находок пока нет.")
            return
        await msg.answer_document(BufferedInputFile(data, filename=f"finds_{msk_now():%Y%m%d}.csv"))

    @router.message(Command("price"))
    async def price(msg: Message, command: CommandObject) -> None:
        parts = (command.args or "").replace("¥", "").split()
        if len(parts) != 2 or not all(p.isdigit() for p in parts) or int(parts[1]) <= 0:
            await msg.answer("/price <номер находки #…> <цена в юанях>, например: /price 123 2300")
            return
        with session_factory() as db:
            res = set_price(db, int(parts[0]), int(parts[1]), settings)
        await msg.answer(res or f"Находка #{html.escape(parts[0])} не найдена")

    @router.message(Command("cats"))
    async def cats(msg: Message) -> None:
        with session_factory() as db:
            chunks = cats_text(db, settings.premium_emoji)
        for chunk in chunks:
            await msg.answer(chunk)

    @router.message(Command("skip"))
    @router.message(Command("unskip"))
    async def skip(msg: Message, command: CommandObject) -> None:
        skip_on = command.command == "skip"
        text = (command.args or "").strip()
        if len(text) < 2:
            await msg.answer(f"/{command.command} текст: часть названия или slug раздела (от 2 букв)")
            return
        with session_factory() as db:
            n = set_skipped(db, text, skip_on)
        what = "выключено" if skip_on else "возвращено"
        await msg.answer(f"{what} категорий: {n}" if n else "Ничего не нашла по этому тексту.")

    @router.callback_query(F.data.startswith("fb:"))
    async def feedback(cb: CallbackQuery) -> None:
        parts = (cb.data or "").split(":")
        value = parts[2] if len(parts) == 3 else ""
        ok = False
        if len(parts) == 3 and parts[1].isdigit() and value in ("1", "-1"):
            with session_factory() as db:
                ok = set_feedback(db, int(parts[1]), int(value))
        await cb.answer(("👍 запомнил" if value == "1" else "👎 учту") if ok else "Находка не найдена")

    @router.message(Command("status"))
    async def status(msg: Message) -> None:
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        job = scheduler.get_job("scan")
        pr = settings.premium_emoji
        lines = [status_header(scanner.blocked, pr), ""]
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
                lines += status_rule(
                    rule.name, state, _hm(rule.last_checked_at), found, new_today, not rule.search_urls, pr
                )
        if job and job.next_run_time and not scanner.paused:
            lines.append(status_next(job.next_run_time, pr))
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
