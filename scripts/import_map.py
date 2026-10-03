"""Карта Avito из avito_niche/catalog.csv -> таблица categories (подкатегории + стартовый приоритет).

Запуск (можно при работающем боте, пишет только в БД):
    PYTHONIOENCODING=utf-8 .venv/Scripts/python -m scripts.import_map [путь к catalog.csv]

Повторный запуск безопасен: подкатегории обновляются по url, история обходов не трогается.
Из-за discovered_at=сейчас раздел 30 дней не перечитывается со страницы раздела (discover_sections).
"""

import csv
import sys
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db import Category, init_db
from app.providers.avito_parser import BASE_URL

DEFAULT_PATH = Path(r"C:\Users\Administrator\Desktop\avito_niche\catalog.csv")


def import_map(session_factory: sessionmaker, path: Path, now: datetime | None = None) -> int:
    now = now or datetime.now()
    with open(path, encoding="utf-8-sig", newline="") as f:
        rows = [r for r in csv.DictReader(f, delimiter=";") if r.get("key", "").startswith("/")]
    with session_factory() as db:
        by_url = {c.url: c for c in db.scalars(select(Category))}
        for r in rows:
            url = BASE_URL + r["key"]
            cat = by_url.get(url) or Category(url=url)
            cat.section = r["key"].split("/")[2]  # /rossiya/<section>/<sub>-<hash>
            cat.name = r["name"][:200]
            cat.discovered_at = now
            cat.prior_score = float(r["score"]) if r.get("score") else None
            db.add(cat)
            by_url[url] = cat
        db.commit()
    return len(rows)


if __name__ == "__main__":
    src = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PATH
    n = import_map(init_db(settings.database_url), src)
    print(f"импортировано подкатегорий: {n} из {src}")
