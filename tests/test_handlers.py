from datetime import datetime
from types import SimpleNamespace

from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.types import Chat, Message, ReplyKeyboardMarkup, Update, User

from app.telegram.handlers import REPORT_BUTTON, STOP_BUTTON, build_router

ADMIN = 777


class FakeSession(BaseSession):
    """Вместо сети: запоминаем запросы к Telegram и отвечаем пустым сообщением."""

    def __init__(self):
        super().__init__()
        self.requests = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.requests.append(method)
        return Message(message_id=1, date=datetime.now(), chat=Chat(id=ADMIN, type="private"))

    async def stream_content(self, *a, **kw):
        yield b""


class FakeCrawler:
    def __init__(self):
        self.calls = []

    def start(self):
        self.calls.append("start")
        return "Начинаю проверку рынка"

    def stop(self):
        self.calls.append("stop")
        return "Проверка не идёт"


def make():
    session = FakeSession()
    bot = Bot("42:TEST", session=session)
    crawler = FakeCrawler()
    dp = Dispatcher()
    dummy = SimpleNamespace(paused=False, blocked=False, busy=False)
    dp.include_router(build_router(ADMIN, dummy, None, None, crawler))
    return SimpleNamespace(bot=bot, dp=dp, session=session, crawler=crawler)


async def send(t, text: str, chat_id: int = ADMIN) -> None:
    msg = Message(
        message_id=1,
        date=datetime.now(),
        chat=Chat(id=chat_id, type="private"),
        from_user=User(id=chat_id, is_bot=False, first_name="x"),
        text=text,
    )
    await t.dp.feed_update(t.bot, Update(update_id=1, message=msg))


async def test_start_buttons():
    t = make()
    await send(t, "/start")
    (req,) = t.session.requests
    assert isinstance(req.reply_markup, ReplyKeyboardMarkup)
    assert [b.text for row in req.reply_markup.keyboard for b in row] == [REPORT_BUTTON, STOP_BUTTON]
    assert "/report" in req.text and "/stop" in req.text


async def test_report_and_stop_commands_and_buttons():
    t = make()
    for text in ("/report", REPORT_BUTTON, "/stop", STOP_BUTTON):
        await send(t, text)
    assert t.crawler.calls == ["start", "start", "stop", "stop"]
    assert [r.text for r in t.session.requests] == ["Начинаю проверку рынка"] * 2 + ["Проверка не идёт"] * 2


async def test_foreign_chat_ignored():
    t = make()
    for text in ("/report", REPORT_BUTTON, "/stop", STOP_BUTTON, "/start"):
        await send(t, text, chat_id=999)
    assert t.crawler.calls == [] and t.session.requests == []  # AC-6.2: чужим бот молчит
