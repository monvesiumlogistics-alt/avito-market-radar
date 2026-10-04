"""История рынка (ADR-015): запись всего, что увидел /report. Вызывающий коммитит сам.

Одна строка на объявление (ads) + события изменений (ad_events), а не снимок на каждую встречу;
замер страницы объявления — card_obs.
"""

from collections.abc import Iterable, Sequence
from datetime import datetime
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import Ad, AdEvent, CardObs
from app.models import Listing
from app.services.market_logic import model_key

POSTED_RANK = {None: 0, "search": 1, "card": 2, "seller": 3}  # дата из профиля точнее карточки, карточка — выдачи


def _event(db: Session, ad_id: str, at: datetime, kind: str, old, new) -> None:
    db.add(AdEvent(ad_id=ad_id, at=at, kind=kind, old=None if old is None else str(old)[:500], new=str(new)[:500]))


def record_search(
    db: Session,
    category_id: int,
    cards: Sequence[Listing],
    promo: Iterable[str],
    now: datetime,
    query: bool = False,
) -> tuple[int, int]:
    """Upsert объявлений со страницы выдачи. Возвращает (новых, уже известных) среди непромо-карточек.

    query=True — выдача поискового среза (ADR-020): объявление запоминает срез (query_id), но настоящей категорией
    становится та, где его увидят в обычной выдаче; для неё такое объявление считается новым."""
    promo = set(promo)
    have = {a.id: a for a in db.scalars(select(Ad).where(Ad.id.in_([c.external_id for c in cards])))}
    new = known = 0
    for c in cards:
        is_promo = c.external_id in promo
        ad = have.get(c.external_id)
        if ad is None:
            ad = Ad(
                id=c.external_id, category_id=category_id, title=c.title[:500], model_key=model_key(c.title),
                price=c.price, url_path=urlsplit(c.url).path[:500], city=c.location, shop=c.seller_name,
                image_url=c.image_url, posted_at=c.published_at, posted_src="search" if c.published_at else None,
                first_seen_at=now, last_seen_at=now, promoted_seen=int(is_promo), status="live",
                query_id=category_id if query else None,
            )  # fmt: skip
            db.add(ad)
            have[ad.id] = ad
            new += not is_promo
            continue
        if query:
            ad.query_id = ad.query_id or category_id
            known += not is_promo
        elif ad.query_id is not None and ad.category_id == ad.query_id:
            ad.category_id = category_id  # до сих пор видели только в срезе: теперь у объявления есть категория
            new += not is_promo
        else:
            known += not is_promo
        ad.last_seen_at = now
        ad.promoted_seen += is_promo
        if c.price is not None and c.price != ad.price:
            _event(db, ad.id, now, "price", ad.price, c.price)
            ad.price = c.price
        if c.title[:500] != ad.title:
            _event(db, ad.id, now, "title", ad.title, c.title[:500])
            ad.title, ad.model_key = c.title[:500], model_key(c.title)
        if ad.status != "live":  # снова в выдаче: живое
            _event(db, ad.id, now, "status", ad.status, "live")
            ad.status, ad.status_at = "live", now
        if ad.shop is None and c.seller_name:
            ad.shop = c.seller_name
    return new, known


def record_card(
    db: Session,
    ad_id: str,
    now: datetime,
    run_id: int | None,
    bucket: str,
    views: int | None,
    today: int | None = None,
    posted_at: datetime | None = None,
    posted_src: str | None = None,
    seller_url: str | None = None,
) -> None:
    """Замер карточки + уточнение даты/продавца объявления (если оно уже есть в ads)."""
    db.add(CardObs(ad_id=ad_id, at=now, run_id=run_id, views=views, today=today, bucket=bucket))
    update_ad(db, ad_id, posted_at, posted_src, seller_url)


def update_ad(
    db: Session, ad_id: str, posted_at: datetime | None = None, posted_src: str | None = None, seller_url=None
) -> None:
    ad = db.get(Ad, ad_id)
    if ad is None:
        return
    if posted_at and POSTED_RANK[posted_src] >= POSTED_RANK[ad.posted_src]:
        ad.posted_at, ad.posted_src = posted_at, posted_src
    if seller_url:
        ad.seller_url = seller_url[:500]


def record_gone(db: Session, ad_id: str, now: datetime) -> None:
    ad = db.get(Ad, ad_id)
    if ad is not None and ad.status == "live":
        _event(db, ad_id, now, "status", "live", "gone")
        ad.status, ad.status_at = "gone", now
