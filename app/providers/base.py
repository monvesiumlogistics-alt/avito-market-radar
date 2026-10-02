from abc import ABC, abstractmethod

from app.models import Listing, SearchUrl


class ProviderBlocked(Exception):
    """Площадка показала блок/капчу. Это не «0 объявлений»: нужен человек."""


class AvitoProvider(ABC):
    """Источник объявлений. Сканеру всё равно, браузер это, API или что-то ещё."""

    async def __aenter__(self) -> "AvitoProvider":
        return self

    async def __aexit__(self, *exc) -> None:
        return None

    @abstractmethod
    async def search(self, search: SearchUrl, page: int = 1) -> list[Listing]: ...
