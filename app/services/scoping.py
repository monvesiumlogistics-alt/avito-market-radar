"""Применение области обхода (app/scope.py) к БД и расчёт ожидаемых загрузок выдачи (ADR-020)."""

import logging
from collections import Counter
from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.config import Settings
from app.db import Category
from app.providers.avito_parser import BASE_URL
from app.scope import EXTRA_QUERIES, SCOPE

log = logging.getLogger(__name__)

CORE_PAGES_PER_DAY = 1.4  # стартовая оценка: крупные — 1 страница, средние — 1–3 до «знакомых»


def key_of(url: str) -> str:
    """Ключ категории: путь (+ запрос) без домена."""
    parts = urlsplit(url)
    return parts.path + (f"?{parts.query}" if parts.query else "")


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
        db.commit()
        counts = Counter(c.scope for c in cats.values())
    if unknown:
        log.warning("[SCOPE] нет в app/scope.py, не обходятся (OFF): %s", ", ".join(unknown))
    return {"counts": dict(counts), "unknown": unknown, "created": created}


def expected_search_loads(counts: dict[str, int], s: Settings, core_pages: float = CORE_PAGES_PER_DAY) -> dict:
    """Ожидаемые страницы выдачи в сутки по областям (без карточек)."""
    raw = {
        "CORE": counts.get("CORE", 0) * core_pages,
        "WATCH": counts.get("WATCH", 0) * 24 / s.watch_every_hours,
        "QUERY": counts.get("QUERY", 0) * 24 / s.query_every_hours,
        "EXPLORE": min(counts.get("EXPLORE", 0) / s.explore_every_days, s.explore_per_run),
    }
    return {k: round(v, 1) for k, v in raw.items()} | {"total": round(sum(raw.values()), 1)}
