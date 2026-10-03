import asyncio
import contextlib
from abc import ABC, abstractmethod
from dataclasses import dataclass

from app.models import Listing, SearchUrl


class ProviderBlocked(Exception):
    """Площадка показала блок/капчу. Это не «0 объявлений»: нужен человек."""


class BrowserLost(Exception):
    """Браузер недоступен (не переоткрылся после уступки, закрыт): дальше по этой подкатегории идти нельзя."""


@dataclass(frozen=True)
class Page:
    html: str
    title: str
    final_url: str


class BrowserGate:
    """Один профиль браузера на двоих (мониторинг и проверка рынка): замок + счётчик ждущих."""

    def __init__(self):
        self._lock = asyncio.Lock()
        self._waiters = 0

    @property
    def locked(self) -> bool:  # используется в тестах
        return self._lock.locked()

    @property
    def contended(self) -> bool:
        """Кто-то ждёт замок: владелец должен отдать браузер."""
        return self._waiters > 0

    async def acquire(self) -> None:
        self._waiters += 1
        try:
            await self._lock.acquire()
        finally:
            self._waiters -= 1

    def release(self) -> None:
        self._lock.release()

    @contextlib.asynccontextmanager
    async def hold(self):
        await self.acquire()
        try:
            yield
        finally:
            self.release()


class AvitoProvider(ABC):
    """Источник объявлений. Сканеру всё равно, браузер это, API или что-то ещё."""

    async def __aenter__(self) -> "AvitoProvider":
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    @abstractmethod
    async def search(self, search: SearchUrl, page: int = 1) -> list[Listing]: ...

    async def fetch(self, url: str, ready_selector: str | None = None) -> Page:
        raise NotImplementedError

    async def wait_unblocked(self, url: str, timeout_s: float, poll_s: float = 5, cancel=None) -> bool:
        """Ждёт, пока человек пройдёт проверку на странице url (ADR-005). По умолчанию не умеет: False."""
        return False
