import re
from typing import Protocol

from app.models import Listing

_NON_WORD = re.compile(r"[^0-9a-zа-я]+")


class RuleLike(Protocol):
    keywords: list[str]
    exclude_keywords: list[str]
    price_min: int | None
    price_max: int | None


def normalize(text: str) -> str:
    """'Dolce&Gabbana', 'Дольче и Габбана' -> 'dolce gabbana', 'дольче габбана'."""
    words = _NON_WORD.split(text.lower().replace("ё", "е"))
    return " ".join(w for w in words if w and w != "и")


def contains(text_norm: str, phrase: str) -> bool:
    p = normalize(phrase)
    if not p:
        return False
    if f" {p} " in f" {text_norm} ":
        return True
    # слитное написание: 'dolcegabbana'
    return " " in p and f" {p.replace(' ', '')} " in f" {text_norm} "


def matches(listing: Listing, rule: RuleLike) -> bool:
    if listing.price is None or listing.price <= 0:
        return False  # "цена не указана"/бесплатно: не наш случай
    if rule.price_min is not None and listing.price < rule.price_min:
        return False
    if rule.price_max is not None and listing.price > rule.price_max:
        return False
    text = normalize(f"{listing.title} {listing.description or ''}")
    if rule.keywords and not any(contains(text, k) for k in rule.keywords):
        return False
    return not any(contains(text, k) for k in rule.exclude_keywords)
