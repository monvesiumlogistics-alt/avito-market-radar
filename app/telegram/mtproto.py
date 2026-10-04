"""Связь с Telegram по MTProto через локальный MTProxy (TgWsProxy), без VPN и без api.telegram.org.

Провайдер режет Bot API (HTTPS), а приложение Telegram ходит через TgWsProxy. Здесь бот ходит так же:
Telethon держит соединение, входящие сообщения/нажатия превращаются в aiogram Update и уходят в Dispatcher,
а вызовы bot.send_message/... исполняются через Telethon. Обработчики aiogram не меняются.
"""

import io
import logging
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageText,
    GetMe,
    SendDocument,
    SendMessage,
    SendPhoto,
    TelegramMethod,
)
from aiogram.types import BufferedInputFile, Chat, InlineKeyboardMarkup, Message, ReplyKeyboardMarkup, Update, User
from telethon import Button, TelegramClient, errors, events
from telethon.network import ConnectionTcpMTProxyRandomizedIntermediate

log = logging.getLogger(__name__)


def _buttons(markup) -> Any:
    if isinstance(markup, ReplyKeyboardMarkup):
        return [[Button.text(b.text, resize=bool(markup.resize_keyboard)) for b in row] for row in markup.keyboard]
    if isinstance(markup, InlineKeyboardMarkup):
        return [[Button.url(b.text, b.url) if b.url else Button.inline(b.text, (b.callback_data or "").encode())
                 for b in row] for row in markup.inline_keyboard]  # fmt: skip
    return None


def _sent(m, chat_id: int) -> Message:
    return Message(message_id=m.id, date=m.date or datetime.now(UTC), chat=Chat(id=chat_id, type="private"))


class MTProtoSession(BaseSession):
    """Исполняет методы Bot API через Telethon. Поддержано ровно то, что вызывает бот."""

    def __init__(self, client: TelegramClient):
        super().__init__()
        self.client = client
        self.callbacks: dict[str, events.CallbackQuery.Event] = {}  # id нажатия -> событие, чтобы ответить
        self.me: User | None = None

    async def close(self) -> None:
        pass  # соединением владеет run_mtproto

    async def stream_content(self, url, headers=None, timeout=30, chunk_size=65536,
                             raise_for_status=True) -> AsyncGenerator[bytes, None]:  # fmt: skip
        raise NotImplementedError
        yield b""

    async def make_request(self, bot: Bot, method: TelegramMethod[Any], timeout: int | None = None) -> Any:
        c = self.client
        if isinstance(method, GetMe):
            return self.me
        if isinstance(method, SendMessage):
            m = await c.send_message(method.chat_id, method.text, parse_mode="html", link_preview=False,
                                     buttons=_buttons(method.reply_markup))  # fmt: skip
            return _sent(m, method.chat_id)
        if isinstance(method, SendPhoto):
            m = await c.send_file(method.chat_id, method.photo, caption=method.caption, parse_mode="html",
                                  buttons=_buttons(method.reply_markup))  # fmt: skip
            return _sent(m, method.chat_id)
        if isinstance(method, SendDocument):
            doc = method.document
            if isinstance(doc, BufferedInputFile):
                doc = io.BytesIO(doc.data)
                doc.name = method.document.filename
            m = await c.send_file(method.chat_id, doc, caption=method.caption, parse_mode="html", force_document=True)
            return _sent(m, method.chat_id)
        if isinstance(method, EditMessageText):
            try:
                m = await c.edit_message(method.chat_id, method.message_id, method.text, parse_mode="html",
                                         link_preview=False, buttons=_buttons(method.reply_markup))  # fmt: skip
            except errors.MessageNotModifiedError as e:
                raise TelegramBadRequest(method, "Bad Request: message is not modified") from e
            return _sent(m, method.chat_id)
        if isinstance(method, AnswerCallbackQuery):
            ev = self.callbacks.pop(method.callback_query_id, None)
            if ev:
                await ev.answer(method.text or "", alert=bool(method.show_alert))
            return True
        raise NotImplementedError(f"MTProto: метод {type(method).__name__} не поддержан")


def make_client(api_id: int, api_hash: str, mtproxy: str, session_path: str) -> TelegramClient:
    """mtproxy = 'host:port:secret' (как в ссылке t.me/proxy)."""
    host, port, secret = mtproxy.split(":")
    return TelegramClient(session_path, api_id, api_hash, connection=ConnectionTcpMTProxyRandomizedIntermediate,
                          proxy=(host, int(port), secret), connection_retries=-1, retry_delay=10,
                          auto_reconnect=True)  # fmt: skip


async def start(client: TelegramClient, bot_token: str) -> tuple[Bot, MTProtoSession]:
    await client.start(bot_token=bot_token)
    session = MTProtoSession(client)
    me = await client.get_me()
    session.me = User(id=me.id, is_bot=True, first_name=me.first_name or "", username=me.username)
    from aiogram.client.default import DefaultBotProperties

    bot = Bot(bot_token, session=session, default=DefaultBotProperties(parse_mode="HTML"))
    return bot, session


def attach(client: TelegramClient, session: MTProtoSession, bot: Bot, dp: Dispatcher) -> None:
    """Входящие сообщения и нажатия инлайн-кнопок -> aiogram Dispatcher."""
    counter = iter(range(1, 10**12))

    def _user(uid: int) -> dict:
        return {"id": uid, "is_bot": False, "first_name": "user"}

    @client.on(events.NewMessage(incoming=True))
    async def on_message(ev) -> None:
        m = ev.message
        upd = {"update_id": next(counter), "message": {
            "message_id": m.id, "date": m.date, "chat": {"id": ev.chat_id, "type": "private"},
            "from": _user(ev.sender_id), "text": m.raw_text or ""}}  # fmt: skip
        await dp.feed_update(bot, Update.model_validate(upd, context={"bot": bot}))

    @client.on(events.CallbackQuery())
    async def on_callback(ev) -> None:
        qid = str(ev.query.query_id)
        session.callbacks[qid] = ev
        upd = {"update_id": next(counter), "callback_query": {
            "id": qid, "from": _user(ev.sender_id), "chat_instance": "", "data": ev.data.decode(errors="ignore"),
            "message": {"message_id": ev.message_id, "date": datetime.now(UTC),
                        "chat": {"id": ev.chat_id, "type": "private"}}}}  # fmt: skip
        try:
            await dp.feed_update(bot, Update.model_validate(upd, context={"bot": bot}))
        finally:
            session.callbacks.pop(qid, None)
