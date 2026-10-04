"""Применение области обхода (app/scope.py) к БД и расчёт ожидаемых загрузок выдачи (ADR-020)."""

import logging
import re
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import Ad, AdQuerySighting, Category
from app.providers.avito_parser import BASE_URL
from app.scope import EXTRA_QUERIES, QUERY_TERMS, SCOPE

log = logging.getLogger(__name__)

CORE_PAGES_PER_DAY = 1.4  # стартовая оценка: крупные — 1 страница, средние — 1–3 до «знакомых»


def key_of(url: str) -> str:
    """Ключ категории: путь (+ запрос) без домена."""
    parts = urlsplit(url)
    return parts.path + (f"?{parts.query}" if parts.query else "")


def _tokens(text: str) -> set[str]:
    t = re.sub(r"['’`´.]", "", text.lower().replace("ё", "е"))
    return set(re.findall(r"[a-zа-я0-9]+", t))


def query_matcher(query_name: str) -> Callable[[str], bool]:
    """Совпадает ли заголовок с запросом QUERY_WATCH. Нет правил для запроса — ничего не совпадает (fail closed)."""
    alternatives = [_tokens(a) for a in QUERY_TERMS.get(query_name.split(" — ")[0], ())]
    return lambda title: any(alt <= _tokens(title) for alt in alternatives)


def apply_scope(session_factory: sessionmaker, now: datetime) -> dict:
    """Разметить все категории БД по SCOPE; неизвестные → OFF (не обходятся, пока их не внесут в scope.py);
    создать недостающие EXTRA_QUERIES. Идемпотентно. Возвращает отчёт для лога/скрипта."""
    unknown, created = [], 0
    with session_factory() as db:
        cats = {key_of(c.url): c for c in db.scalars(select(Category))}
        for key, (scope, name, section, flags) in EXTRA_QUERIES.items():
            if key not in cats:
                cats[key] = Category(section=section, name=name, url=BASE_URL + key, discovered_at=now)
                db.add(cats[key])
                created += 1
            cats[key].scope, cats[key].kind, cats[key].flags = scope, "query", flags or None
        for key, cat in cats.items():
            if key in EXTRA_QUERIES:
                continue
            entry = SCOPE.get(key)
            if entry is None:
                unknown.append(key)
                cat.scope = "OFF"
                continue
            scope, _, flags, dup = entry
            cat.scope, cat.flags, cat.duplicate_of = scope, flags or None, dup
            cat.kind = "query" if "?q=" in key else "category"
            if scope == "CORE" and not cat.scope_status:
                cat.scope_status = "hypothesis"  # CORE_CONFIRMED — по данным (Product Hunter)
        db.flush()
        _release_query_owned(db)
        db.commit()
        counts = Counter(c.scope for c in cats.values())
    if unknown:
        log.warning("[SCOPE] нет в app/scope.py, не обходятся (OFF): %s", ", ".join(unknown))
    return {"counts": dict(counts), "unknown": unknown, "created": created}


def _release_query_owned(db) -> None:
    """ADR-021: объявления, которые раньше «принадлежали» запросу (category_id = запрос), → sighting + category NULL."""
    query_ids = {c.id for c in db.scalars(select(Category).where(Category.kind == "query"))}
    for ad in db.scalars(select(Ad).where(Ad.category_id.in_(query_ids))):
        if db.get(AdQuerySighting, (ad.id, ad.category_id)) is None:
            db.add(AdQuerySighting(ad_id=ad.id, query_category_id=ad.category_id, first_seen_at=ad.first_seen_at,
                                   last_seen_at=ad.last_seen_at))  # fmt: skip
        ad.category_id = None


def expected_search_loads(counts: dict[str, int], s: Settings, core_pages: float = CORE_PAGES_PER_DAY) -> dict:
    """Ожидаемые страницы выдачи в сутки по областям (без карточек)."""
    raw = {
        "CORE": counts.get("CORE", 0) * core_pages,
        "WATCH": counts.get("WATCH", 0) * 24 / s.watch_every_hours,
        "QUERY": counts.get("QUERY", 0) * 24 / s.query_every_hours,
        "EXPLORE": min(counts.get("EXPLORE", 0) / s.explore_every_days, s.explore_per_run),
    }
    return {k: round(v, 1) for k, v in raw.items()} | {"total": round(sum(raw.values()), 1)}
