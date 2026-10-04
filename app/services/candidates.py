"""Product Candidate Engine (ADR-023): какие товары стоит проверить карточками в Phase 4 — только по данным выдачи.

Кандидат ≠ «товар продаётся». Кандидат = «по дешёвым сигналам выдачи товар достаточно интересен, чтобы потратить
на него загрузку карточки». Ноль запросов к Avito: только ads / products / categories / ad_query_sightings.

Пути: REPEATED (одна модель в нескольких разных объявлениях) и EMERGING (новая модель по надёжной дате).
ATTENTION — Phase 4 (нужна база категории по карточкам). Никакого итогового «балла»: причины + метрики,
порядок — детерминированный по понятным признакам.
"""

import statistics
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import Ad, AdQuerySighting, Category, Product, ScanCategory

ELIGIBLE_SCOPES = ("CORE", "WATCH", "QUERY", "EXPLORE")
PRICE_FLOOR = 8000
LEVELS = ("LOW", "MEDIUM", "HIGH")
DATE_RANK = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
# пороги — стартовые, настраиваются по отчёту (scripts/product_candidates)
REPEATED_MIN = 3  # разных органических объявлений за 14 дн
EMERGING_MAX_AGE = 5  # дней с первой надёжной даты на рынке
EMERGING_MIN_72H = 2
GROWTH_MIN_HISTORY = 7  # дней истории бота, раньше рост не утверждаем
EMERGING_PRIOR_DAYS = 3  # «новый» — только если категорию видели ≥ 3 дн до первого появления товара и его там не было


@dataclass
class AdRow:
    id: str
    product_id: int
    category_id: int | None
    scopes: frozenset
    price: int
    city: str
    shop: str | None
    promoted: bool
    market_at: datetime  # дата на рынке: posted_at, иначе first_seen_at
    date_conf: str  # HIGH карточка/профиль · MEDIUM выдача (день; может быть «поднятие») · LOW first_seen бота
    last_seen_at: datetime
    live: bool


@dataclass
class Metrics:
    product_id: int
    name: str
    identity: str
    listings_24h: int = 0
    listings_72h: int = 0
    listings_7d: int = 0
    listings_14d: int = 0
    organic_14d: int = 0
    promoted_14d: int = 0
    organic_72h_reliable: int = 0
    live_listings: int = 0
    distinct_ads: int = 0
    cities_7d: int = 0
    cities_14d: int = 0
    known_shops_7d: int = 0
    known_shops_14d: int = 0
    seller_data_coverage: float = 0.0
    top_shop_share: float = 0.0
    days_spread_14d: int = 0
    price_p25: int | None = None
    price_median: int | None = None
    price_p75: int | None = None
    first_market_seen: datetime | None = None
    first_date_conf: str = "LOW"
    latest_market_seen: datetime | None = None
    product_age_days: float | None = None
    reliable_date_share: float = 0.0
    recent_3d: int = 0
    previous_3d: int = 0
    categories: list[str] = field(default_factory=list)
    scopes: list[str] = field(default_factory=list)
    query_sightings: int = 0
    prior_coverage_days: float = 0.0  # сколько дней до первого появления товара данные его категорий уже были


@dataclass
class Candidate:
    metrics: Metrics
    reasons: list[str]  # REPEATED, EMERGING, MULTI_CITY, MULTI_SHOP, SPREAD_DAYS, GROWTH, FROM_ZERO, HIGH_TICKET
    independence: str
    confidence: str  # candidate_confidence — сила рыночных сигналов, НЕ уверенность в модели (metrics.identity)
    why: list[str]  # человекочитаемые причины и понижения


# --- загрузка ---


def _city(url_path: str) -> str:
    return url_path.strip("/").split("/")[0]


def _market_date(ad: Ad) -> tuple[datetime, str]:
    if ad.posted_at and ad.posted_src in ("card", "seller"):
        return ad.posted_at, "HIGH"
    if ad.posted_at and ad.posted_src == "search":
        return ad.posted_at, "MEDIUM"
    return ad.first_seen_at, "LOW"  # бот впервые увидел ≠ объявление появилось


def load_rows(db: Session) -> tuple[list[AdRow], dict[int, Product], dict[int, Category], dict[int, int]]:
    """Объявления с распознанным товаром (HIGH/MEDIUM), ценой ≥ 8000 и хотя бы одной допустимой областью.
    Одно объявление — одна строка, сколько бы раз его ни видели в категории и запросах."""
    cats = {c.id: c for c in db.scalars(select(Category))}
    sight: dict[str, set[int]] = defaultdict(set)
    for ad_id, qid in db.execute(select(AdQuerySighting.ad_id, AdQuerySighting.query_category_id)):
        sight[ad_id].add(qid)
    rows, query_hits = [], Counter()
    for ad in db.scalars(select(Ad).where(Ad.product_id.is_not(None), Ad.identity_conf.in_(("HIGH", "MEDIUM")))):
        scopes = {cats[ad.category_id].scope} if ad.category_id in cats else set()
        scopes |= {cats[q].scope for q in sight.get(ad.id, ()) if q in cats}
        scopes &= set(ELIGIBLE_SCOPES)
        if not scopes or not ad.price or ad.price < PRICE_FLOOR:
            continue
        query_hits[ad.product_id] += len(sight.get(ad.id, ()))
        at, conf = _market_date(ad)
        rows.append(AdRow(ad.id, ad.product_id, ad.category_id, frozenset(scopes), ad.price, _city(ad.url_path),
                          ad.shop, ad.promoted_seen > 0, at, conf, ad.last_seen_at, ad.status == "live"))  # fmt: skip
    products = {p.id: p for p in db.scalars(select(Product).where(Product.id.in_({r.product_id for r in rows})))}
    return rows, products, cats, dict(query_hits)


def history_days(db: Session, now: datetime) -> float:
    """Сколько дней бот уже наблюдает рынок: самое раннее из первого обхода и первого увиденного объявления."""
    firsts = [x for x in (db.scalar(select(func.min(ScanCategory.at))), db.scalar(select(func.min(Ad.first_seen_at))))
              if x]  # fmt: skip
    return max(0.0, (now - min(firsts)).total_seconds() / 86400) if firsts else 0.0


# --- метрики ---


def _q(values: list[int], q: float) -> int | None:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(q * (len(v) - 1) + 0.5))]


def product_metrics(
    pid: int, ads: list[AdRow], product: Product, cats: dict, now: datetime, hits: int, data_since: dict
) -> Metrics:
    def within(days: float) -> list[AdRow]:
        return [a for a in ads if now - timedelta(days=days) <= a.market_at <= now]

    w14, w7 = within(14), within(7)
    org14 = [a for a in w14 if not a.promoted]
    shops14 = [a.shop for a in w14 if a.shop]
    first = min(ads, key=lambda a: a.market_at)
    prices = [a.price for a in w14] or [a.price for a in ads]
    reliable = [a for a in ads if DATE_RANK[a.date_conf] >= 1]
    m = Metrics(pid, product.display_name, product.confidence)
    m.listings_24h, m.listings_72h, m.listings_7d, m.listings_14d = len(within(1)), len(within(3)), len(w7), len(w14)
    m.organic_14d, m.promoted_14d = len(org14), len(w14) - len(org14)
    m.organic_72h_reliable = sum(1 for a in within(3) if not a.promoted and DATE_RANK[a.date_conf] >= 1)
    m.live_listings, m.distinct_ads = sum(a.live for a in ads), len(ads)
    m.cities_7d, m.cities_14d = len({a.city for a in w7}), len({a.city for a in w14})
    m.known_shops_7d, m.known_shops_14d = len({a.shop for a in w7 if a.shop}), len(set(shops14))
    m.seller_data_coverage = round(len(shops14) / len(w14), 2) if w14 else 0.0
    m.top_shop_share = round(Counter(shops14).most_common(1)[0][1] / len(w14), 2) if shops14 else 0.0
    m.days_spread_14d = len({a.market_at.date() for a in w14})
    m.price_p25, m.price_median, m.price_p75 = _q(prices, 0.25), int(statistics.median(prices)), _q(prices, 0.75)
    m.first_market_seen, m.first_date_conf = first.market_at, first.date_conf
    m.latest_market_seen = max(a.last_seen_at for a in ads)
    m.product_age_days = round((now - first.market_at).total_seconds() / 86400, 1)
    m.reliable_date_share = round(len(reliable) / len(ads), 2)
    m.recent_3d = sum(1 for a in within(3) if not a.promoted)
    m.previous_3d = sum(
        1 for a in ads if not a.promoted and now - timedelta(days=6) <= a.market_at < now - timedelta(days=3)
    )
    m.categories = sorted({cats[a.category_id].name for a in ads if a.category_id in cats})
    m.scopes = sorted({s for a in ads for s in a.scopes})
    m.query_sightings = hits
    since = [data_since[c] for c in {a.category_id for a in ads} if c in data_since]
    if since:  # насколько глубоко назад мы вообще видим рынок этих категорий до первого появления товара
        m.prior_coverage_days = round(max(0.0, (first.market_at - min(since)).total_seconds() / 86400), 1)
    return m


# --- правила ---


def _down(level: str, steps: int = 1) -> str:
    return LEVELS[max(0, LEVELS.index(level) - steps)]


def independence(m: Metrics) -> tuple[str, str]:
    """Насколько объявления похожи на разных продавцов. Продавец частника в выдаче неизвестен — только косвенно."""
    if m.cities_14d >= 3 or m.known_shops_14d >= 3 or (m.cities_14d >= 2 and m.days_spread_14d >= 3):
        return "HIGH", f"{m.cities_14d} городов · {m.known_shops_14d} магазинов · {m.days_spread_14d} разных дней"
    if (m.cities_14d <= 1 and m.days_spread_14d <= 1 and m.known_shops_14d <= 1) or (
        m.top_shop_share >= 0.7 and m.seller_data_coverage >= 0.5
    ):
        return "LOW", "похоже на одну загрузку: один город/день или один магазин"
    return "MEDIUM", "несколько объявлений, продавцы известны не полностью"


def evaluate(m: Metrics, hist_days: float, category_p75: int | None) -> tuple[Candidate | None, str | None]:
    """(кандидат, None) или (None, причина отказа)."""
    reasons, why = [], []
    if m.listings_14d == 0:
        return None, "only_old_listings"
    repeated = m.organic_14d >= REPEATED_MIN
    if repeated:
        reasons.append("REPEATED")
        why.append(f"{m.organic_14d} разных органических объявлений за 14 дн")
    growth_ok = hist_days >= GROWTH_MIN_HISTORY
    growth = growth_ok and m.recent_3d >= 3 and m.previous_3d >= 1 and m.recent_3d >= 2 * m.previous_3d
    from_zero = growth_ok and m.previous_3d == 0 and m.recent_3d >= 2
    young = m.product_age_days is not None and m.product_age_days <= EMERGING_MAX_AGE
    dated = DATE_RANK[m.first_date_conf] >= 1  # первое появление известно по дате рынка, а не по боту
    watched = m.prior_coverage_days >= EMERGING_PRIOR_DAYS  # до появления категорию уже видели — и товара не было
    if young and (m.organic_72h_reliable >= EMERGING_MIN_72H or growth or from_zero):
        if dated and watched:
            reasons.append("EMERGING")
            why.append(f"новая модель: на рынке {m.product_age_days} дн, до этого {m.prior_coverage_days} дн "
                       f"наблюдений без неё; за 72 ч {m.organic_72h_reliable} объявл.")  # fmt: skip
        elif not repeated:
            return None, "insufficient_reliable_date" if not dated else "insufficient_prior_coverage"
    if growth:
        reasons.append("GROWTH")
        why.append(f"за 3 дня {m.recent_3d} против {m.previous_3d} за предыдущие 3")
    if from_zero and "EMERGING" in reasons:
        reasons.append("FROM_ZERO")
    if not {"REPEATED", "EMERGING"} & set(reasons):
        if m.listings_14d >= REPEATED_MIN and m.organic_14d < REPEATED_MIN:
            return None, "mostly_promoted"
        return None, "single_listing" if m.organic_14d <= 1 else "two_listings_not_new"
    level, ind_why = independence(m)
    if m.cities_14d >= 3:
        reasons.append("MULTI_CITY")
    if m.known_shops_14d >= 2:
        reasons.append("MULTI_SHOP")
    if m.days_spread_14d >= 3:
        reasons.append("SPREAD_DAYS")
    if category_p75 and m.price_median and m.price_median >= category_p75:
        reasons.append("HIGH_TICKET")  # только усиливает уже найденного кандидата
    # уверенность кандидата: от независимости, с понятными понижениями
    if "REPEATED" in reasons:
        conf = level if m.organic_14d >= 5 else min(level, "MEDIUM", key=LEVELS.index)
    else:
        conf = "MEDIUM" if m.cities_14d >= 2 else "LOW"
    why.append(f"независимость {level}: {ind_why}")
    if m.identity == "MEDIUM":
        conf = _down(conf)
        why.append("модель распознана с уверенностью MEDIUM → −1")
    if m.reliable_date_share < 0.5:
        conf = _down(conf)
        why.append(f"надёжные даты только у {int(m.reliable_date_share * 100)}% объявлений → −1")
    if hist_days < 2 and conf == "HIGH":
        conf = "MEDIUM"
        why.append(f"история наблюдений {hist_days:.1f} дн — не больше MEDIUM")
    return Candidate(m, reasons, level, conf, why), None


def priority(c: Candidate) -> tuple:
    """Детерминированный порядок (без скрытого балла): оба пути → повтор → новое; затем уверенность, независимость,
    органические объявления, свежесть, чек как последний tie-break. Область — не преимущество."""
    r = set(c.reasons)
    path = 0 if {"REPEATED", "EMERGING"} <= r else 1 if "REPEATED" in r else 2
    m = c.metrics
    return (path, -LEVELS.index(c.confidence), -LEVELS.index(c.independence), -m.organic_14d,
            -(m.latest_market_seen.timestamp() if m.latest_market_seen else 0), -(m.price_median or 0))  # fmt: skip


def run(db: Session, now: datetime) -> dict:
    """Оценить все товары. Ни одной загрузки Avito."""
    rows, products, cats, hits = load_rows(db)
    hist = history_days(db, now)
    by_product: dict[int, list[AdRow]] = defaultdict(list)
    for r in rows:
        by_product[r.product_id].append(r)
    cat_prices: dict[int, list[int]] = defaultdict(list)
    for r in rows:
        if r.category_id and now - timedelta(days=14) <= r.market_at <= now:
            cat_prices[r.category_id].append(r.price)
    candidates, rejects = [], Counter()
    rejected: list[tuple[str, str]] = []
    data_since: dict[int, datetime] = {}
    for r in rows:  # с какого момента рынка у нас есть данные по категории
        if r.category_id and (r.category_id not in data_since or r.market_at < data_since[r.category_id]):
            data_since[r.category_id] = r.market_at
    for pid, ads in by_product.items():
        m = product_metrics(pid, ads, products[pid], cats, now, hits.get(pid, 0), data_since)
        main_cat = Counter(a.category_id for a in ads if a.category_id).most_common(1)
        p75 = _q(cat_prices.get(main_cat[0][0], []), 0.75) if main_cat else None
        cand, reject = evaluate(m, hist, p75)
        if cand:
            candidates.append(cand)
        else:
            rejects[reject] += 1
            rejected.append((m.name, reject))
    candidates.sort(key=priority)
    return {"history_days": round(hist, 1), "eligible_ads": len(rows), "products": len(by_product),
            "candidates": candidates, "rejects": rejects, "rejected": rejected}  # fmt: skip
