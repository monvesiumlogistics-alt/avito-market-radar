import html
import logging

from aiogram import Bot
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from app.db import ListingRow

log = logging.getLogger(__name__)


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
        lines += ["", f"Опубликовано: ~{row.published_at:%d.%m %H:%M}"]
    return "\n".join(lines)


class TelegramNotifier:
    def __init__(self, bot: Bot, chat_id: int):
        self.bot = bot
        self.chat_id = chat_id

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
            await self.bot.send_message(self.chat_id, text, reply_markup=kb, disable_web_page_preview=True)
            log.info("[NOTIFY] telegram success avito_id=%s", row.external_id)
            return True
        except Exception:
            log.exception("[NOTIFY] telegram failed avito_id=%s, повторю на следующем скане", row.external_id)
            return False

    async def send_text(self, text: str) -> None:
        try:
            await self.bot.send_message(self.chat_id, text)
        except Exception:
            log.exception("[NOTIFY] не удалось отправить служебное сообщение")
