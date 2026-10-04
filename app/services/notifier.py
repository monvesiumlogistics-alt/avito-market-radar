import html
import json
import logging
from datetime import time
from pathlib import Path

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions

from app.db import ListingRow

log = logging.getLogger(__name__)

NO_PREVIEW = LinkPreviewOptions(is_disabled=True)


def format_price(price: int | None) -> str:
    return "цена не указана" if price is None else f"{price:,}".replace(",", " ") + " ₽"


def format_listing(row: ListingRow, rule_name: str) -> str:
    lines = ["🔥 <b>Новое объявление</b>", "", f"<b>{html.escape(row.title[:200])}</b>", ""]
    lines.append(f"💰 {format_price(row.price)}")
    if row.location:
        lines.append(f"📍 {html.escape(row.location)}")
    if row.category:
        lines.append(f"👤 {html.escape(row.category)}")
    lines.append(f"🏷 {html.escape(rule_name)}")
    if row.seller_name:
        lines.append(f"🛍 {html.escape(row.seller_name[:60])}")
    if row.published_at:
        fmt = "%d.%m" if row.published_at.time() == time() else "%d.%m %H:%M"  # 00:00 = в выдаче был только день
        lines += ["", f"Опубликовано: ~{row.published_at:{fmt}}"]
    return "\n".join(lines)


class TelegramNotifier:
    def __init__(self, bot: Bot, chat_id: int, outbox: str | None = "./data/telegram_outbox.jsonl"):
        self.bot = bot
        self.chat_id = chat_id
        # Telegram недоступен (например, без VPN): служебные сообщения не теряются — копятся в файле и досылаются
        self.outbox = Path(outbox) if outbox else None

    async def send_listing(self, row: ListingRow, rule_name: str) -> bool:
        text = format_listing(row, rule_name)
        kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть на Avito", url=row.url)]])
        if row.image_url:
            try:
                await self.bot.send_photo(self.chat_id, photo=row.image_url, caption=text, reply_markup=kb)
                log.info("[NOTIFY] telegram success (photo) avito_id=%s", row.external_id)
                return True
            except Exception as e:  # Telegram не смог скачать фото с CDN и т.п. -> шлём текстом
                log.warning("[NOTIFY] фото не ушло (%s), шлю текстом", e)
        try:
            await self.bot.send_message(self.chat_id, text, reply_markup=kb, link_preview_options=NO_PREVIEW)
            log.info("[NOTIFY] telegram success avito_id=%s", row.external_id)
            return True
        except Exception:
            log.exception("[NOTIFY] telegram failed avito_id=%s, повторю на следующем скане", row.external_id)
            return False

    async def send_text(self, text: str, markup=None) -> int | None:
        """Одно сообщение (не режет длинный текст — это делает вызывающий). Возвращает id или None при ошибке."""
        try:
            msg = await self.bot.send_message(
                self.chat_id, text, reply_markup=markup, link_preview_options=NO_PREVIEW
            )
            return msg.message_id
        except Exception:
            log.exception("[NOTIFY] не удалось отправить служебное сообщение — в очередь")
            self._queue(text)
            return None

    def _queue(self, text: str) -> None:
        if self.outbox is None:
            return
        self.outbox.parent.mkdir(parents=True, exist_ok=True)
        with self.outbox.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"text": text}, ensure_ascii=False) + "\n")

    async def flush_outbox(self) -> int:
        """Дослать накопленное по порядку; на первой ошибке остановиться (связи всё ещё нет). Возвращает отправлено."""
        if self.outbox is None or not self.outbox.exists():
            return 0
        pending = [json.loads(x)["text"] for x in self.outbox.read_text(encoding="utf-8").splitlines() if x.strip()]
        sent = 0
        for text in pending:
            try:
                await self.bot.send_message(self.chat_id, text, link_preview_options=NO_PREVIEW)
            except Exception as e:
                log.warning("[NOTIFY] очередь: Telegram всё ещё недоступен (%s), осталось %d", e, len(pending) - sent)
                break
            sent += 1
        rest = pending[sent:]
        if rest:
            self.outbox.write_text("".join(json.dumps({"text": t}, ensure_ascii=False) + "\n" for t in rest),
                                   encoding="utf-8")  # fmt: skip
        else:
            self.outbox.unlink()
        if sent:
            log.info("[NOTIFY] из очереди отправлено %d", sent)
        return sent

    async def edit_text(self, message_id: int, text: str) -> None:
        try:
            await self.bot.edit_message_text(
                text, chat_id=self.chat_id, message_id=message_id, link_preview_options=NO_PREVIEW
            )
        except TelegramBadRequest as e:
            if "message is not modified" not in str(e):
                log.exception("[NOTIFY] не удалось изменить сообщение")
        except Exception:
            log.exception("[NOTIFY] не удалось изменить сообщение")
