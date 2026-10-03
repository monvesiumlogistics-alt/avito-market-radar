from datetime import datetime
from types import SimpleNamespace

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText

from app.services.notifier import TelegramNotifier, format_listing


class FakeBot:
    def __init__(self, error=None):
        self.error, self.calls = error, []

    async def send_message(self, chat_id, text, **kw):
        self.calls.append(("send", chat_id, text, kw))
        if self.error:
            raise self.error
        return SimpleNamespace(message_id=42)

    async def edit_message_text(self, text, **kw):
        self.calls.append(("edit", text, kw))
        if self.error:
            raise self.error


def bad_request(text):
    return TelegramBadRequest(EditMessageText(text="x"), text)


async def test_send_text_returns_id_and_disables_preview():
    bot = FakeBot()
    assert await TelegramNotifier(bot, 7).send_text("hi") == 42
    assert bot.calls[0][3]["link_preview_options"].is_disabled is True


async def test_send_text_error_logged_returns_none():
    assert await TelegramNotifier(FakeBot(RuntimeError("boom")), 7).send_text("hi") is None


async def test_edit_text_ok_and_preview_disabled():
    bot = FakeBot()
    await TelegramNotifier(bot, 7).edit_text(42, "new")
    kw = bot.calls[0][2]
    assert (kw["chat_id"], kw["message_id"]) == (7, 42) and kw["link_preview_options"].is_disabled is True


async def test_edit_text_not_modified_swallowed_silently(caplog):
    bot = FakeBot(bad_request("Bad Request: message is not modified: specified new message content..."))
    await TelegramNotifier(bot, 7).edit_text(42, "same")
    assert not caplog.records


async def test_edit_text_other_errors_logged_not_raised(caplog):
    await TelegramNotifier(FakeBot(bad_request("Bad Request: message to edit not found")), 7).edit_text(42, "x")
    await TelegramNotifier(FakeBot(RuntimeError("net")), 7).edit_text(42, "x")
    assert len(caplog.records) == 2


def row(published_at):
    return SimpleNamespace(
        title="t", price=1, location=None, category=None, seller_name=None, published_at=published_at
    )


def test_format_listing_hides_midnight_time():
    assert format_listing(row(datetime(2026, 10, 2)), "r").endswith("Опубликовано: ~02.10")
    assert format_listing(row(datetime(2026, 10, 2, 22, 36)), "r").endswith("Опубликовано: ~02.10 22:36")
