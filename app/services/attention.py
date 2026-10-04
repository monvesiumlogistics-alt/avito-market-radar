"""Phase 4 (ADR-025): норма просмотров категории и Attention Multiple товара — по замерам карточек (card_obs).

Два непересекающихся смысла выборки:
  BASELINE — нейтральные объявления категории (случайные/подряд), норма рынка;
  CANDIDATE — объявления конкретного кандидата.
Норма не строится на карточках сильных кандидатов (иначе она смещена вверх). Замеры, отобранные по результату
(backfill — находки с vpd ≥ 50, recheck — их перепроверка), не используются нигде.

ATTENTION = медиана vpd товара / медиана vpd нормы категории — только если норма пригодна (≥ BASELINE_MIN_N
разных объявлений). Одна карточка ≥ 3× нормы — ATTENTION_ANOMALY (нужно подтверждение второй), не доказанный спрос.
"""

import statistics
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Ad, CardObs
from app.services.market_logic import age_days, calc_vpd

BASELINE_BUCKETS = ("baseline", "report")  # нейтральная выборка категории
PRODUCT_BUCKETS = ("baseline", "report", "candidate", "followup")  # любые нейтральные замеры объявлений товара
EXCLUDED_BUCKETS = ("backfill", "recheck")  # отобраны по результату — смещены
BASELINE_MIN_N = 8
ANOMALY_MULTIPLE = 3.0
DATE_CONF = {"card": "HIGH", "seller": "HIGH", "search": "MEDIUM"}


@dataclass(frozen=True)
class Observation:
    ad_id: str
    at: datetime
    views: int
    vpd: int
    date_conf: str
    bucket: str


@dataclass(frozen=True)
class Baseline:
    category_id: int
    n: int
    median: float | None
    p75: float | None
    days: int
    confidence: str  # NONE | LOW | MEDIUM | HIGH

    @property
    def usable(self) -> bool:
        return self.n >= BASELINE_MIN_N and bool(self.median)


@dataclass(frozen=True)
class Attention:
    product_id: int
    obs_n: int  # разные объявления (external_id) с замером
    vpd_median: float | None
    vpd_p75: float | None
    baseline: Baseline | None
    multiple: float | None
    status: str  # NO_OBS | BASELINE_PENDING | SINGLE | ATTENTION_ANOMALY | MEASURED
    confidence: str  # LOW | MEDIUM | HIGH (для NO_OBS / BASELINE_PENDING — NONE)


def _p75(values: list[float]) -> float | None:
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(0.75 * (len(v) - 1) + 0.5))]


def latest_obs(db: Session, buckets: tuple[str, ...], ad_ids: set[str] | None = None) -> dict[str, Observation]:
    """Последний пригодный замер на объявление: одно объявление = одно наблюдение (dedup по external_id)."""
    q = select(CardObs, Ad).join(Ad, Ad.id == CardObs.ad_id).where(CardObs.bucket.in_(buckets),
                                                                     CardObs.views.is_not(None))  # fmt: skip
    if ad_ids is not None:
        q = q.where(CardObs.ad_id.in_(ad_ids))
    out: dict[str, Observation] = {}
    for o, ad in db.execute(q):
        if not ad.posted_at or ad.posted_at > o.at:
            continue  # без даты публикации просмотры в день не посчитать
        cur = out.get(o.ad_id)
        if cur is None or o.at > cur.at:
            vpd = calc_vpd(o.views, age_days(ad.posted_at, o.at))
            out[o.ad_id] = Observation(o.ad_id, o.at, o.views, vpd, DATE_CONF.get(ad.posted_src, "LOW"), o.bucket)
    return out


def baseline(db: Session, category_id: int) -> Baseline:
    """Норма категории: нейтральные замеры её объявлений ≥ 8 000 ₽."""
    ids = set(db.scalars(select(Ad.id).where(Ad.category_id == category_id, Ad.price >= 8000)))
    obs = list(latest_obs(db, BASELINE_BUCKETS, ids).values()) if ids else []
    vpds = [o.vpd for o in obs]
    days = len({o.at.date() for o in obs})
    n = len(obs)
    if n < BASELINE_MIN_N:
        conf = "NONE"
    elif n >= 15 and days >= 2:
        conf = "HIGH"
    elif days >= 2 or n >= 12:
        conf = "MEDIUM"
    else:
        conf = "LOW"  # пригодна, но один день замеров
    return Baseline(category_id, n, statistics.median(vpds) if vpds else None, _p75(vpds), days, conf)


def product_attention(db: Session, product_id: int, base: Baseline | None) -> Attention:
    """Внимание к товару относительно нормы его основной категории."""
    ids = set(db.scalars(select(Ad.id).where(Ad.product_id == product_id)))
    obs = list(latest_obs(db, PRODUCT_BUCKETS, ids).values())
    if not obs:
        return Attention(product_id, 0, None, None, base, None, "NO_OBS", "NONE")
    vpds = [o.vpd for o in obs]
    med, p75 = statistics.median(vpds), _p75(vpds)
    if base is None or not base.usable:
        return Attention(product_id, len(obs), med, p75, base, None, "BASELINE_PENDING", "NONE")
    multiple = round(med / base.median, 2)
    if len(obs) == 1:
        status = "ATTENTION_ANOMALY" if multiple >= ANOMALY_MULTIPLE else "SINGLE"
        return Attention(product_id, 1, med, p75, base, multiple, status, "LOW")
    conf = "HIGH" if len(obs) >= 3 and base.confidence in ("MEDIUM", "HIGH") else "MEDIUM"
    return Attention(product_id, len(obs), med, p75, base, multiple, "MEASURED", conf)


def main_category(db: Session, product_id: int) -> int | None:
    """Категория, где у товара больше всего объявлений (норма берётся по ней)."""
    counts: dict[int, int] = defaultdict(int)
    for cid in db.scalars(select(Ad.category_id).where(Ad.product_id == product_id, Ad.category_id.is_not(None))):
        counts[cid] += 1
    return max(counts, key=counts.get) if counts else None
