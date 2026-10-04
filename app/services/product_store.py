"""Хранение product identity (ADR-022): товар объявления, журнал смен, офлайн-пересборка и диагностика.

Ни одной загрузки Avito: всё строится из сохранённых ads.title.
"""

import statistics
from collections import Counter, defaultdict
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import Ad, AdQuerySighting, Category, Product, ProductAssignment, ProductCandidate
from app.services.products import (
    PRODUCT_EXTRACTOR_VERSION,
    ProductIdentity,
    _is_variant,
    domain_of,
    identify,
    normalize,
)

CONF_RANK = {"UNKNOWN": 0, "LOW": 1, "MEDIUM": 2, "HIGH": 3}


def _product(db: Session, ident: ProductIdentity, ad: Ad, now: datetime, cache: dict) -> Product:
    p = cache.get(ident.canonical_key) or db.scalar(
        select(Product).where(Product.canonical_key == ident.canonical_key)
    )
    if p is None:
        p = Product(
            canonical_key=ident.canonical_key, brand=ident.brand, model=ident.model, display_name=ident.display_name,
            confidence=ident.confidence, first_seen_at=ad.first_seen_at or now, last_seen_at=ad.last_seen_at or now,
            extractor_version=PRODUCT_EXTRACTOR_VERSION, created_from=ad.title[:500],
        )  # fmt: skip
        db.add(p)
        db.flush()
    elif CONF_RANK[ident.confidence] > CONF_RANK[p.confidence]:
        p.confidence = ident.confidence
    p.first_seen_at = min(p.first_seen_at, ad.first_seen_at or now)  # = MIN по объявлениям товара
    p.last_seen_at = max(p.last_seen_at, ad.last_seen_at or now)  # = MAX: когда товар последний раз был на рынке
    cache[ident.canonical_key] = p
    return p


def assign_product(db: Session, ad: Ad, domain: str, now: datetime, cache: dict | None = None) -> ProductIdentity:
    """Определить товар объявления и записать: HIGH/MEDIUM → product_id; LOW/UNKNOWN → product_id = NULL
    (бренд остаётся для диагностики). Смена товара пишется в product_assignments."""
    cache = {} if cache is None else cache
    ident = identify(ad.title, domain)
    product = _product(db, ident, ad, now, cache) if ident.clusterable else None
    new_id = product.id if product else None
    if new_id != ad.product_id:  # смена товара (в т.ч. первое назначение) — в журнал
        db.add(ProductAssignment(ad_id=ad.id, product_id=new_id, confidence=ident.confidence, method="extractor",
                                 extractor_version=PRODUCT_EXTRACTOR_VERSION, assigned_at=now))  # fmt: skip
    ad.product_id, ad.identity_conf = new_id, ident.confidence
    ad.identity_brand, ad.identity_variant = ident.brand, (ident.variant or None) and ident.variant[:200]
    ad.extractor_version = PRODUCT_EXTRACTOR_VERSION
    return ident


def touch_product(db: Session, ad: Ad) -> None:
    """Объявление снова видели: last_seen товара = MAX(last_seen его объявлений)."""
    if ad.product_id:
        p = db.get(Product, ad.product_id)
        if p is not None and ad.last_seen_at and ad.last_seen_at > p.last_seen_at:
            p.last_seen_at = ad.last_seen_at


def ad_domains(db: Session) -> dict[str, str]:
    """Домен разбора для каждого объявления: по настоящей категории, иначе по категории запроса."""
    urls = dict(db.execute(select(Category.id, Category.url)).all())
    out = {ad_id: domain_of(urls.get(cid)) for ad_id, cid in db.execute(select(Ad.id, Ad.category_id))}
    for ad_id, qid in db.execute(select(AdQuerySighting.ad_id, AdQuerySighting.query_category_id)):
        if out.get(ad_id) == "generic":
            out[ad_id] = domain_of(urls.get(qid))
    return out


def rebuild(db: Session, now: datetime) -> dict:
    """Офлайн: переопределить товары всех объявлений текущим экстрактором; удалить товары без объявлений."""
    domains = ad_domains(db)
    cache: dict = {}
    changed = 0
    for ad in db.scalars(select(Ad)):
        before = ad.product_id
        assign_product(db, ad, domains.get(ad.id, "generic"), now, cache)
        changed += before != ad.product_id
    db.flush()
    seen = {
        pid: (lo, hi)
        for pid, lo, hi in db.execute(
            select(Ad.product_id, func.min(Ad.first_seen_at), func.max(Ad.last_seen_at))
            .where(Ad.product_id.is_not(None))
            .group_by(Ad.product_id)
        )
    }  # first/last seen товара — точно из его объявлений (не накопленные значения прошлых версий)
    orphans = []
    for p in db.scalars(select(Product)):
        if p.id in seen:
            p.first_seen_at, p.last_seen_at = seen[p.id]
        else:
            orphans.append(p)
    for p in orphans:
        db.delete(p)
    gone = {p.id for p in orphans}
    stale = [c for c in db.scalars(select(ProductCandidate)) if c.product_id in gone]
    for c in stale:  # текущее состояние кандидата без товара удаляем; история (candidate_log) остаётся
        db.delete(c)
    db.commit()
    return {"ads": len(domains), "changed": changed, "orphans_removed": len(orphans),
            "stale_candidates_removed": len(stale)}  # fmt: skip


def diagnostics(db: Session, scopes: tuple[str, ...] = ("CORE", "WATCH", "QUERY"), top: int = 20) -> dict:
    """Качество: доли уверенности, кластеры (разные external_id на товар), подозрительные кластеры."""
    scoped = set(db.scalars(select(Category.id).where(Category.scope.in_(scopes))))
    in_query = set(
        db.scalars(select(AdQuerySighting.ad_id).where(AdQuerySighting.query_category_id.in_(scoped)))
    )  # fmt: skip
    ads = [a for a in db.scalars(select(Ad)) if a.category_id in scoped or a.id in in_query]
    conf = Counter(a.identity_conf or "UNKNOWN" for a in ads)
    by_product: dict[int, list[Ad]] = defaultdict(list)
    for a in ads:
        if a.product_id:
            by_product[a.product_id].append(a)
    products = {p.id: p for p in db.scalars(select(Product).where(Product.id.in_(list(by_product))))}
    sizes = sorted(((len(v), pid) for pid, v in by_product.items()), reverse=True)
    domains = ad_domains(db)
    suspicious = []
    for n, pid in sizes:
        cluster = by_product[pid]
        flags = _suspicious(cluster, products[pid].canonical_key, domains.get(cluster[0].id, "generic"))
        if flags:
            suspicious.append((products[pid].display_name, n, flags))
    total = len(ads) or 1
    return {
        "ads": len(ads),
        "share": {k: round(100 * conf.get(k, 0) / total, 1) for k in ("HIGH", "MEDIUM", "LOW", "UNKNOWN")},
        "products": len(by_product),
        "median_listings": statistics.median(n for n, _ in sizes) if sizes else 0,
        "singletons": sum(n == 1 for n, _ in sizes),
        "top": [(products[pid].display_name, n) for n, pid in sizes[:top]],
        "suspicious": suspicious,
        "brand_only": Counter(a.identity_brand for a in ads if a.identity_conf == "LOW").most_common(10),
        "total_products_db": db.scalar(select(func.count()).select_from(Product)),
    }


def _key_parts(key: str) -> set[str]:
    """Части сохранённого ключа товара и их склейки по две (x16 = x + 16)."""
    parts = key.split("|")[1].split("-") if "|" in key else []
    return set(parts) | {a + b for a, b in zip(parts, parts[1:], strict=False)}


def _suspicious(ads: list[Ad], key: str, domain: str) -> list[str]:
    """Признаки ложного слияния: огромный разброс цены; разные коды с цифрами в заголовках одного товара
    (кроме характеристик) — возможно, под одним ключом несколько моделей."""
    flags = []
    if len(ads) >= 5:
        codes: Counter = Counter()
        for a in ads:
            toks = normalize(a.title)
            codes.update({t for t in toks if any(c.isdigit() for c in t) and not _is_variant(t, domain)})
        key_parts = _key_parts(key)
        common = {t for t, n in codes.items() if n == len(ads)}  # код самой модели есть во всех
        odd = [t for t, n in codes.items() if t not in common and n >= 2 and t not in key_parts
               and not (t.isdigit() and len(t) <= 2)]  # «x 16» vs «x16», «Ryzen 5» — не другие модели  # fmt: skip
        if len(odd) >= 3:
            flags.append("разные коды в заголовках: " + ", ".join(sorted(odd)[:6]))
    prices = sorted(a.price for a in ads if a.price)
    if len(prices) >= 4:
        lo, hi = prices[len(prices) // 4], prices[(3 * len(prices)) // 4]
        if lo and hi / lo > 3:
            flags.append(f"разброс цены p75/p25 = {hi / lo:.1f}")
    return flags
