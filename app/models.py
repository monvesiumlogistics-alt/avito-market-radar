from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel, Field


@dataclass(frozen=True)
class SearchUrl:
    label: str
    url: str


def parse_search_urls(raw: str) -> list[SearchUrl]:
    """Формат 'Метка|URL;Метка|URL', метка необязательна."""
    result = []
    for chunk in raw.replace("\n", ";").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        label, sep, url = chunk.partition("|")
        result.append(SearchUrl(label.strip(), url.strip()) if sep else SearchUrl("", label.strip()))
    return result


class Listing(BaseModel):
    """Нормализованное объявление, не зависит от источника."""

    external_id: str
    source: str = "avito"
    title: str
    description: str | None = None
    price: int | None = None
    currency: str = "RUB"
    url: str
    image_url: str | None = None
    location: str | None = None
    seller_name: str | None = None
    category: str | None = None  # метка search URL ("Мужское" и т.п.)
    published_at: datetime | None = None
    parsed_at: datetime = Field(default_factory=datetime.now)
