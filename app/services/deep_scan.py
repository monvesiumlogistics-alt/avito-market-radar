"""Phase 4 (ADR-025): какие карточки открыть — норма категорий, затем кандидаты, затем вторые карточки сильным.

Чистое планирование по БД (без загрузок). Загрузки делает MarketCrawler (вид прогона «deep») через общий
AvitoTraffic. Successive sampling: 1 карточка на товар → второй замер только у сильных/неясных → третий при
большом разбросе. Ни одного профиля продавца.
"""

import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Ad, CardObs, Category
from app.services.attention import BASELINE_MIN_N, baseline, latest_obs, main_category, product_attention
from app.services.candidates import Candidate

REOPEN_HOURS = 24  # одно объявление не открывать чаще
BASELINE_BUDGET = 16  # две категории до пригодной нормы (8 + 8)
FIRST_PASS = 22  # товаров в первом проходе (по 1 карточке)
BRAND_CAP = 4  # в первом проходе — не больше товаров одного бренда, пока остальные не получили шанс
WEAK_MULTIPLE = 0.8  # после первой карточки слабее — вторую не тратить
SPREAD_FOR_THIRD = 3.0  # две карточки расходятся в 3+ раза — третья
DATE_RANK = {"card": 2, "seller": 2, "search": 1}


@dataclass(frozen=True)
class Pick:
    ad_id: str
    url_path: str
    bucket: str  # baseline | candidate | followup
    product_id: int | None
    category_id: int | None


def _stable(ad_id: str) -> str:
    """Детерминированно-равномерный порядок (вместо random): одинаковый при повторе, без перекоса по id."""
    return hashlib.sha1(ad_id.encode()).hexdigest()


def product_obs(db: Session, product_id: int) -> dict:
    """Нейтральные замеры объявлений товара (одно объявление = одно наблюдение)."""
    ids = set(db.scalars(select(Ad.id).where(Ad.product_id == product_id)))
    return latest_obs(db, ("baseline", "report", "candidate", "followup"), ids)


def recently_opened(db: Session, now: datetime) -> set[str]:
    since = now - timedelta(hours=REOPEN_HOURS)
    return set(db.scalars(select(CardObs.ad_id).where(CardObs.at >= since)))


def _age_ok(ad: Ad, now: datetime, lo: float = 1, hi: float = 7) -> bool:
    if not ad.posted_at:
        return False
    days = (now - ad.posted_at).total_seconds() / 86400
    return lo <= days <= hi


def plan_baseline(db: Session, now: datetime, candidates: list[Candidate], budget: int = BASELINE_BUDGET) -> list[Pick]:
    """Норма тех категорий, где больше всего кандидатов. Категорию добираем, только если хватит бюджета довести её
    до пригодной (≥ BASELINE_MIN_N), — полупустая норма бесполезна. Объявления: нейтральные, не кандидаты."""
    cand_products = {c.metrics.product_id for c in candidates}
    per_cat = Counter(cid for c in candidates if (cid := main_category(db, c.metrics.product_id)))
    opened = recently_opened(db, now)
    picks: list[Pick] = []
    for cid, _ in per_cat.most_common():
        cat = db.get(Category, cid)
        if cat is None or cat.kind != "category":
            continue
        need = max(0, BASELINE_MIN_N - baseline(db, cid).n)
        if need == 0 or need > budget - len(picks):
            continue
        pool = [
            a for a in db.scalars(select(Ad).where(Ad.category_id == cid, Ad.price >= 8000, Ad.promoted_seen == 0))
            if a.product_id not in cand_products and a.id not in opened and _age_ok(a, now)
            and DATE_RANK.get(a.posted_src, 0) >= 1
        ]  # fmt: skip
        pool.sort(key=lambda a: _stable(a.id))
        chosen, shops = [], Counter()
        for a in pool:
            if a.shop and shops[a.shop] >= 2:
                continue  # не набирать норму из одного магазина
            chosen.append(a)
            shops[a.shop] += 1
            if len(chosen) == need:
                break
        if len(chosen) == need:
            picks += [Pick(a.id, a.url_path, "baseline", None, cid) for a in chosen]
    return picks


def best_listing(db: Session, product_id: int, now: datetime, opened: set[str]) -> Ad | None:
    """Объявление товара для замера: не видели продвинутым → надёжная дата → 1–7 дн → новый город/магазин."""
    ads = [a for a in db.scalars(select(Ad).where(Ad.product_id == product_id, Ad.price >= 8000)) if a.id not in opened]
    seen = product_obs(db, product_id)
    seen_ads = [db.get(Ad, i) for i in seen]
    seen_cities = {a.url_path.strip("/").split("/")[0] for a in seen_ads if a}
    seen_shops = {a.shop for a in seen_ads if a and a.shop}
    ads = [a for a in ads if a.id not in seen]

    def key(a: Ad) -> tuple:
        city = a.url_path.strip("/").split("/")[0]
        return (a.promoted_seen > 0, -DATE_RANK.get(a.posted_src, 0), not _age_ok(a, now), city in seen_cities,
                bool(a.shop) and a.shop in seen_shops, _stable(a.id))  # fmt: skip

    return min(ads, key=key) if ads else None


def plan_first_pass(db: Session, now: datetime, candidates: list[Candidate], limit: int = FIRST_PASS,
                    planned_baseline: set[int] = frozenset()) -> list[Pick]:  # fmt: skip
    """По 1 карточке на товар; сначала товары категорий с (будущей) нормой; не больше BRAND_CAP на бренд."""
    opened = recently_opened(db, now)
    order = sorted(candidates, key=lambda c: (main_category(db, c.metrics.product_id) not in planned_baseline,
                                              c.confidence == "LOW"))  # fmt: skip — сортировка стабильна (Phase 3)
    brands, picks = Counter(), []
    for c in order:
        pid = c.metrics.product_id
        if product_obs(db, pid):
            continue  # уже есть замер — не тратить первую загрузку
        brand = c.metrics.canonical_key.split("|")[0]
        if brands[brand] >= BRAND_CAP:
            continue
        ad = best_listing(db, pid, now, opened)
        if ad is None:
            continue
        picks.append(Pick(ad.id, ad.url_path, "candidate", pid, main_category(db, pid)))
        brands[brand] += 1
        if len(picks) >= limit:
            break
    return picks


def plan_followups(db: Session, now: datetime, candidates: list[Candidate], limit: int) -> list[Pick]:
    """Вторая карточка — товарам, которые после первой не слабые (≥ 0.8× нормы); третья — при разбросе ≥ 3×.
    Без пригодной нормы вторую не тратим: сравнить не с чем."""
    opened = recently_opened(db, now)
    bases = {}
    wants: list[tuple[float, int]] = []
    for c in candidates:
        pid = c.metrics.product_id
        cid = main_category(db, pid)
        if cid not in bases:
            bases[cid] = baseline(db, cid) if cid else None
        att = product_attention(db, pid, bases[cid])
        if att.multiple is None or att.multiple < WEAK_MULTIPLE:
            continue
        if att.obs_n == 1:
            wants.append((att.multiple, pid))
        elif att.obs_n == 2:
            vpds = [o.vpd for o in product_obs(db, pid).values()]
            if min(vpds) and max(vpds) / min(vpds) >= SPREAD_FOR_THIRD:
                wants.append((att.multiple, pid))
    picks = []
    for _, pid in sorted(wants, reverse=True):
        ad = best_listing(db, pid, now, opened)
        if ad is not None:
            picks.append(Pick(ad.id, ad.url_path, "followup", pid, main_category(db, pid)))
        if len(picks) >= limit:
            break
    return picks


def observation_counts(db: Session, candidates: list[Candidate]) -> Counter:
    """Сколько товаров получили 0 / 1 / 2 / 3+ замеров (разных объявлений)."""
    out = Counter()
    for c in candidates:
        n = len(product_obs(db, c.metrics.product_id))
        out["3+" if n >= 3 else str(n)] += 1
    return out


def categories_of(db: Session, picks: list[Pick]) -> dict[int, int]:
    out: dict[int, int] = defaultdict(int)
    for p in picks:
        if p.category_id:
            out[p.category_id] += 1
    return dict(out)
