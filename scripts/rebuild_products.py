"""Офлайн-пересборка товаров из сохранённых заголовков (ADR-022) + отчёт о качестве кластеризации.

    PYTHONIOENCODING=utf-8 .venv/Scripts/python -m scripts.rebuild_products [--dry-run]

Ни одной загрузки Avito: браузер и Playwright не импортируются. Новая версия экстрактора = перезапуск этого
скрипта, рынок заново сканировать не нужно. --dry-run — посчитать и показать, ничего не записывая.
"""

import sys

from app.config import settings
from app.db import init_db
from app.providers.avito_parser import msk_now
from app.services.product_store import diagnostics, rebuild
from app.services.products import PRODUCT_EXTRACTOR_VERSION


def report(d: dict, title: str) -> None:
    print(f"\n== {title}: объявлений {d['ads']}, товаров {d['products']} (в базе всего {d['total_products_db']})")
    print("уверенность, %:", d["share"])
    print(f"медиана объявлений на товар: {d['median_listings']} · товаров из 1 объявления: {d['singletons']}")
    print("крупнейшие кластеры:")
    for name, n in d["top"]:
        print(f"  {n:4}  {name}")
    print("подозрительные кластеры:" if d["suspicious"] else "подозрительных кластеров нет")
    for name, n, flags in d["suspicious"]:
        print(f"  {name} ({n}): {'; '.join(flags)}")
    print("только бренд (LOW), топ:", d["brand_only"])


if __name__ == "__main__":
    dry = "--dry-run" in sys.argv
    sf = init_db(settings.database_url)
    with sf() as db:
        result = rebuild(db, msk_now()) if not dry else None
        if dry:
            db.rollback()
    print(f"экстрактор v{PRODUCT_EXTRACTOR_VERSION}; пересборка: {result or 'dry-run'}")
    with sf() as db:
        report(diagnostics(db), "CORE + WATCH + QUERY")
        report(diagnostics(db, scopes=("CORE", "WATCH", "QUERY", "EXPLORE", "OFF", "HARD_EXCLUDE")), "все объявления")
