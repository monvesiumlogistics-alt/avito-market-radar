"""Разметить БД по app/scope.py и проверить инвариант: каждая категория карты ровно в одной области (ADR-020).

    PYTHONIOENCODING=utf-8 .venv/Scripts/python -m scripts.check_scope

Пишет только поля области в categories (то же делает бот при старте). Печатает разбивку и ожидаемые загрузки выдачи.
"""

import sys
from collections import Counter

from sqlalchemy import select

from app.config import settings
from app.db import Category, init_db
from app.providers.avito_parser import msk_now
from app.services.scoping import apply_scope, expected_search_loads, key_of

if __name__ == "__main__":
    sf = init_db(settings.database_url)
    report = apply_scope(sf, msk_now())
    with sf() as db:
        cats = list(db.scalars(select(Category)))
    real = [c for c in cats if c.kind == "category"]
    queries = [c for c in cats if c.kind == "query"]
    print("категории:", dict(Counter(c.scope for c in real)), f"всего {len(real)}")
    print("запросы:", dict(Counter(c.scope for c in queries)), f"всего {len(queries)}")
    print("QUERY_WATCH:", ", ".join(sorted(c.name for c in queries if c.scope == "QUERY")))
    active = Counter(c.scope for c in cats if not c.skipped)
    print("загрузок выдачи в сутки (оценка):", expected_search_loads(active, settings))
    if report["unknown"]:
        sys.exit(f"НЕ РАЗМЕЧЕНЫ (нет в app/scope.py): {report['unknown']}")
    print(f"инвариант ok: {len({key_of(c.url) for c in real})}/{len(real)} категорий в ровно одной области")
