"""ADR-025: выборочные карточки — норма категории отдельно от кандидатов, successive sampling, бюджет, без профилей."""

from datetime import timedelta
from types import SimpleNamespace as NS

from sqlalchemy import select

from app.db import Ad, CardObs, Category, CrawlRun, Product
from app.services.deep_scan import (
    BRAND_CAP,
    plan_baseline,
    plan_first_pass,
    plan_followups,
)
from tests.test_market import NOW, item_html, life, rows

D = timedelta(days=1)


def world(tmp_path, n_products=1, per_product=3, neutral=10, brand="gopro", **settings):
    t = life(tmp_path, {"A": [900]}, **settings)
    with t.sf() as db:
        cid = db.scalars(select(Category)).first().id
        for p in range(1, n_products + 1):
            db.add(Product(id=p, canonical_key=f"{brand}|m{p}", brand=brand, model=f"m{p}",
                           display_name=f"{brand} m{p}", confidence="HIGH", first_seen_at=NOW, last_seen_at=NOW,
                           extractor_version=2))  # fmt: skip
            for k in range(per_product):
                i = p * 100 + k
                db.add(Ad(id=str(i), category_id=cid, title=f"{brand} m{p}", url_path=f"/city{k}/x/t_{i}", price=30000,
                          posted_at=NOW - 2 * D, posted_src="search", first_seen_at=NOW, last_seen_at=NOW,
                          product_id=p, identity_conf="HIGH"))  # fmt: skip
        for i in range(neutral):
            db.add(Ad(id=str(5000 + i), category_id=cid, title="камера", url_path=f"/n{i}/x/t_{5000 + i}", price=20000,
                      posted_at=NOW - 3 * D, posted_src="search", first_seen_at=NOW, last_seen_at=NOW,
                      shop="S" if i < 4 else None))  # fmt: skip
        db.commit()
    return t, cid


def cand(pid, brand="gopro", conf="MEDIUM"):
    return NS(metrics=NS(product_id=pid, canonical_key=f"{brand}|m{pid}"), confidence=conf)


def test_baseline_excludes_candidates_promoted_recent_and_one_shop(tmp_path):
    t, cid = world(tmp_path, neutral=14)
    with t.sf() as db:
        db.get(Ad, "5004").promoted_seen = 1
        db.add(CardObs(ad_id="5005", at=NOW - timedelta(hours=3), views=5, bucket="baseline"))  # открывали недавно
        db.commit()
        picks = plan_baseline(db, NOW, [cand(1)])
    ids = {p.ad_id for p in picks}
    assert len(picks) == 7 and all(p.bucket == "baseline" for p in picks)  # 8 − уже есть 1 замер
    assert not ids & {"100", "101", "102", "5004", "5005"}  # не кандидат, не продвинутое, не открытое за сутки
    with t.sf() as db:
        assert sum(db.get(Ad, i).shop == "S" for i in ids) <= 2  # норма не из одного магазина


def test_baseline_skips_category_if_budget_cannot_make_it_usable(tmp_path):
    t, _ = world(tmp_path)
    with t.sf() as db:
        assert plan_baseline(db, NOW, [cand(1)], budget=5) == []  # полупустая норма бесполезна


def test_first_pass_one_card_per_product_and_brand_cap(tmp_path):
    t, _ = world(tmp_path, n_products=6)
    with t.sf() as db:
        picks = plan_first_pass(db, NOW, [cand(p) for p in range(1, 7)])
    assert len(picks) == BRAND_CAP and len({p.product_id for p in picks}) == BRAND_CAP
    assert all(p.bucket == "candidate" for p in picks)


def test_followup_only_for_not_weak(tmp_path):
    t, cid = world(tmp_path, n_products=2)
    with t.sf() as db:
        for i in range(8):  # норма: vpd 20 (60 просмотров за 3 дня)
            db.add(CardObs(ad_id=str(5000 + i), at=NOW, views=60, bucket="baseline"))
        db.add(CardObs(ad_id="100", at=NOW, views=200, bucket="candidate"))  # товар 1: vpd 100 = 5×
        db.add(CardObs(ad_id="200", at=NOW, views=20, bucket="candidate"))  # товар 2: vpd 10 = 0.5× — слабый
        db.commit()
        picks = plan_followups(db, NOW + timedelta(hours=1), [cand(1), cand(2)], limit=10)
    assert [(p.product_id, p.bucket) for p in picks] == [(1, "followup")]
    assert picks[0].ad_id in {"101", "102"}  # другое объявление, не то же самое


async def test_deep_run_budget_buckets_no_seller_profiles(tmp_path):
    t, cid = world(tmp_path, n_products=2, neutral=10, deep_budget=12)
    with t.sf() as db:
        ads = db.scalars(select(Ad)).all()
        for a in ads:
            views = 600 if a.product_id == 1 else 60
            t.pages["https://www.avito.ru" + a.url_path] = item_html(views, date="1 октября в 12:00")
        db.commit()
    t.crawler.start("deep")
    await t.crawler._task
    (run,) = rows(t, CrawlRun)
    assert run.kind == "deep" and run.loads <= 12
    assert not [u for u in t.provider.calls if "/brands/" in u or "/user/" in u]  # 0 профилей продавцов
    assert len(t.provider.calls) == len(set(t.provider.calls))  # ни одна карточка не открыта дважды
    obs = rows(t, CardObs)
    buckets = {o.bucket for o in obs}
    assert buckets <= {"baseline", "candidate", "followup"} and "baseline" in buckets and "candidate" in buckets
    assert "🔬 Выборочные карточки" in t.notifier.sent[-1]
