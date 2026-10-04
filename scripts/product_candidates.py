# ruff: noqa: E501 — форматирование отчёта
"""Офлайн-отчёт Product Candidate Engine (ADR-023): какие товары стоит проверить карточками в Phase 4.

    PYTHONIOENCODING=utf-8 .venv/Scripts/python -m scripts.product_candidates [--save] [--top 30]

Ни одной загрузки Avito: браузер и Playwright не импортируются. Без --save — только показать (dry run);
с --save — записать product_candidates / candidate_log.
"""

import sys
from collections import Counter

from app.config import settings
from app.db import init_db
from app.providers.avito_parser import msk_now
from app.services.candidates import GROWTH_MIN_HISTORY, Candidate, persist, run

REJECT_TEXT = {
    "single_listing": "одно объявление",
    "two_listings_not_new": "два объявления, товар не новый",
    "only_old_listings": "только старые объявления (дата рынка > 14 дн)",
    "mostly_promoted": "в основном продвинутые объявления",
    "insufficient_reliable_date": "нет надёжной даты рынка (только «бот впервые увидел»)",
    "insufficient_prior_coverage": "мало истории до появления товара — новизну не доказать",
}


def rub(v):
    return f"{v // 1000}k" if v else "—"


def line(i: int, c: Candidate) -> str:
    m = c.metrics
    return (f"{i:2}. {m.name} [{', '.join(m.scopes)}] — {'+'.join(c.reasons)} · 14д {m.listings_14d} "
            f"(орг. {m.organic_14d}) · городов {m.cities_14d} · магазинов {m.known_shops_14d} · "
            f"{rub(m.price_median)} · {c.confidence}/{c.independence}")  # fmt: skip


def detail(i: int, c: Candidate) -> str:
    m = c.metrics
    return "\n".join([
        f"{i}. {m.name}",
        f"   причины: {', '.join(c.reasons)}",
        f"   объявлений: 24ч {m.listings_24h} · 72ч {m.listings_72h} · 7д {m.listings_7d} · 14д {m.listings_14d}"
        f" · живых {m.live_listings} · всего {m.distinct_ads}",
        f"   органические {m.organic_14d} · продвинутые {m.promoted_14d} · дней с объявлениями {m.days_spread_14d}",
        f"   городов 7д/14д: {m.cities_7d}/{m.cities_14d} · известных магазинов {m.known_shops_7d}/{m.known_shops_14d}"
        f" · покрытие продавцов {int(m.seller_data_coverage * 100)}% · доля крупнейшего магазина {int(m.top_shop_share * 100)}%",
        f"   цена: {rub(m.price_p25)} / {rub(m.price_median)} / {rub(m.price_p75)}",
        f"   на рынке с {m.first_market_seen:%d.%m %H:%M} (дата {m.first_date_conf}), последний раз"
        f" {m.latest_market_seen:%d.%m %H:%M}, возраст {m.product_age_days} дн · надёжных дат {int(m.reliable_date_share * 100)}%",
        f"   категории: {', '.join(m.categories) or '—'} · области: {', '.join(m.scopes)} · находок через запросы {m.query_sightings}",
        f"   модель: {m.identity} · кандидат: {c.confidence} · независимость: {c.independence}",
        *[f"   - {w}" for w in c.why],
    ])  # fmt: skip


if __name__ == "__main__":
    save = "--save" in sys.argv
    top = int(sys.argv[sys.argv.index("--top") + 1]) if "--top" in sys.argv else 30
    now = msk_now()
    sf = init_db(settings.database_url)
    with sf() as db:
        r = run(db, now)
        saved = persist(db, r["candidates"], now) if save else None
    cands: list[Candidate] = r["candidates"]
    h = r["history_days"]
    print("== DATA MATURITY")
    print(f"история наблюдений: {h} дн · подходящих объявлений {r['eligible_ads']} · распознанных товаров {r['products']}")
    print(f"REPEATED: доступен · EMERGING: {'доступен' if h >= 3 else 'почти недоступен — мало истории до появления'}"
          f" · GROWTH: {'доступен' if h >= GROWTH_MIN_HISTORY else f'нет (нужно ≥ {GROWTH_MIN_HISTORY} дн)'}")  # fmt: skip
    print(f"\n== КАНДИДАТЫ: {len(cands)} из {r['products']} товаров" + (f" · сохранено {saved}" if save else " (dry run)"))
    print("по пути:", dict(Counter("REPEATED+EMERGING" if {"REPEATED", "EMERGING"} <= set(c.reasons) else
                                   "REPEATED" if "REPEATED" in c.reasons else "EMERGING" for c in cands)))  # fmt: skip
    print("по уверенности кандидата:", dict(Counter(c.confidence for c in cands)))
    print("по независимости:", dict(Counter(c.independence for c in cands)))
    print("по областям:", dict(Counter(s for c in cands for s in c.metrics.scopes)))
    print("\n== ПОЧЕМУ НЕ КАНДИДАТЫ")
    for k, n in r["rejects"].most_common():
        print(f"  {n:4}  {REJECT_TEXT.get(k, k)}")
    print(f"\n== TOP {top}")
    for i, c in enumerate(cands[:top], 1):
        print(line(i, c))
    print("\n== TOP 10 подробно")
    for i, c in enumerate(cands[:10], 1):
        print(detail(i, c))
