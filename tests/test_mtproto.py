"""MTProto-переходник: вызовы aiogram Bot исполняются через Telethon-клиент (здесь — заглушка)."""

from datetime import UTC, datetime

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from telethon import errors

from app.telegram.handlers import KEYBOARD
from app.telegram.mtproto import MTProtoSession


class FakeClient:
    def __init__(self):
        self.calls = []

    async def send_message(self, chat, text, **kw):
        self.calls.append(("send", chat, text, kw))
        return type("M", (), {"id": 7, "date": datetime.now(UTC)})()

    async def edit_message(self, chat, mid, text, **kw):
        raise errors.MessageNotModifiedError(request=None)


class FakeCallback:
    def __init__(self):
        self.answered = None

    async def answer(self, text, alert=False):
        self.answered = text


async def test_send_edit_and_callback_via_telethon():
    client = FakeClient()
    s = MTProtoSession(client)
    bot = Bot("1:" + "a" * 35, session=s)
    msg = await bot.send_message(5, "<b>привет</b>", reply_markup=KEYBOARD)
    assert msg.message_id == 7
    _, chat, text, kw = client.calls[0]
    assert (chat, text, kw["parse_mode"], kw["link_preview"]) == (5, "<b>привет</b>", "html", False)
    labels = [b.button.text for row in kw["buttons"] for b in row]
    assert labels == ["🔎 Проверить рынок", "🔬 Проверить кандидатов", "⏹ Стоп"]
    kb = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="👍", callback_data="fb:1:1")]])
    await bot.send_message(5, "x", reply_markup=kb)
    assert client.calls[1][3]["buttons"][0][0].type.data == b"fb:1:1"
    with pytest.raises(TelegramBadRequest, match="message is not modified"):
        await bot.edit_message_text("same", chat_id=5, message_id=7)
    s.callbacks["42"] = cb = FakeCallback()
    assert await bot.answer_callback_query("42", text="👍 запомнил") is True and cb.answered == "👍 запомнил"
