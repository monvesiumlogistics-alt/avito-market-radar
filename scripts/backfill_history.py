"""Находки (finds), собранные до истории рынка, -> ads + card_obs (ADR-015). Без заходов на Avito.

Запуск (можно при работающем боте, пишет только в БД):
    PYTHONIOENCODING=utf-8 .venv/Scripts/python -m scripts.backfill_history

Повторный запуск безопасен: объявления, у которых уже есть замер bucket=backfill, пропускаются.
"""

from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db import Ad, CardObs, Find, init_db
from app.services.history import record_gone
from app.services.market_logic import model_key


def backfill(session_factory: sessionmaker) -> int:
    """Возвращает, сколько объявлений перенесено."""
    with session_factory() as db:
        done = set(db.scalars(select(CardObs.ad_id).where(CardObs.bucket == "backfill")))
        moved = 0
        for f in db.scalars(select(Find).order_by(Find.created_at)):
            if f.external_id in done:
                continue
            done.add(f.external_id)
            moved += 1
            if db.get(Ad, f.external_id) is None:
                posted, src = (f.seller_date, "seller") if f.seller_date else (f.page_date, "card")
                db.add(
                    Ad(
                        id=f.external_id, category_id=f.category_id, title=f.title, model_key=model_key(f.title),
                        price=f.price_min, url_path=urlsplit(f.url).path, posted_at=posted,
                        posted_src=src if posted else None, first_seen_at=f.created_at,
                        last_seen_at=f.last_checked_at or f.created_at, status="live",
                    )
                )  # fmt: skip
                db.flush()
            db.add(CardObs(ad_id=f.external_id, at=f.created_at, run_id=f.run_id, views=f.views, today=f.today,
                           bucket="backfill"))  # fmt: skip
            if f.last_checked_at and f.views_last is not None:
                db.add(CardObs(ad_id=f.external_id, at=f.last_checked_at, views=f.views_last, bucket="backfill"))
            if f.gone_at:
                record_gone(db, f.external_id, f.gone_at)
        db.commit()
    return moved


if __name__ == "__main__":
    print(f"перенесено объявлений: {backfill(init_db(settings.database_url))}")
