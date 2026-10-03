import pytest

from app.config import settings


@pytest.fixture(autouse=True)
def plain_emoji(monkeypatch):
    """Глобальный settings читает .env разработчика (PREMIUM_EMOJI=true): тесты по умолчанию — обычные эмодзи."""
    monkeypatch.setattr(settings, "premium_emoji", False)
