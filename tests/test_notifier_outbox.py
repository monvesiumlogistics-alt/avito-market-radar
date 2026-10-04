"""Telegram недоступен: служебные сообщения копятся в файле и досылаются по порядку, когда связь вернётся."""

from app.services.notifier import TelegramNotifier


class FlakyBot:
    def __init__(self):
        self.up, self.sent = False, []

    async def send_message(self, chat_id, text, **kw):
        if not self.up:
            raise ConnectionError("telegram unreachable")
        self.sent.append(text)
        return type("M", (), {"message_id": len(self.sent)})()


async def test_outbox_queues_and_flushes_in_order(tmp_path):
    bot = FlakyBot()
    n = TelegramNotifier(bot, 1, outbox=str(tmp_path / "outbox.jsonl"))
    assert await n.send_text("итог 1") is None and await n.send_text("итог 2 «кавычки»") is None
    assert await n.flush_outbox() == 0  # связи всё ещё нет — ничего не теряем
    bot.up = True
    assert await n.flush_outbox() == 2 and bot.sent == ["итог 1", "итог 2 «кавычки»"]
    assert not (tmp_path / "outbox.jsonl").exists() and await n.flush_outbox() == 0
