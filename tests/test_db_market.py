from datetime import datetime

from sqlalchemy import inspect, select

from app.db import Category, CrawlRun, Find, init_db


def _factory():
    return init_db("sqlite:///:memory:")


def _seed(db, **find_kw):
    now = datetime(2026, 10, 3, 12, 0)
    cat = Category(section="telefony", name="Apple", url="/rossiya/telefony/apple", discovered_at=now)
    run = CrawlRun(started_at=now)
    db.add_all([cat, run])
    db.flush()
    kw = dict(
        run_id=run.id,
        category_id=cat.id,
        group_key="iphone 15",
        title="iPhone 15",
        price_min=50000,
        price_max=55000,
        vpd=120,
        age_days=2.0,
        date_checked=True,
        copies=2,
        url="https://www.avito.ru/x_1",
        external_id="1",
        hot=True,
        created_at=now,
    )
    db.add(Find(**kw | find_kw))
    db.commit()


def test_create_all_adds_three_tables_keeps_old():
    names = set(inspect(_factory().kw["bind"]).get_table_names())
    assert {"categories", "crawl_runs", "finds", "watch_rules", "listings"} <= names


def test_find_persists_views_page_date_seller_date():
    page, seller = datetime(2026, 10, 1, 22, 36), datetime(2026, 10, 1, 22, 40)
    with _factory()() as db:
        _seed(db, views=180, today=40, page_date=page, seller_date=seller)
        f = db.scalar(select(Find))
        assert (f.views, f.today, f.page_date, f.seller_date, f.sent) == (180, 40, page, seller, False)


def test_finds_china_price_nullable():
    with _factory()() as db:
        _seed(db)
        f = db.scalar(select(Find))
        assert f.china_price is None and f.views is None and f.seller_date is None
