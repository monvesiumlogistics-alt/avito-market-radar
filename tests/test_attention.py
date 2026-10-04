"""ADR-025: норма категории и Attention Multiple — раздельные выборки, без смещения и ложной точности."""

from datetime import timedelta

from sqlalchemy import select

from app.db import Ad, CardObs, Category, Product
from app.services.attention import baseline, latest_obs, main_category, product_attention
from tests.test_market import NOW, life

D = timedelta(days=1)


def setup_cat(tmp_path):
    t = life(tmp_path, {"A": [900]})
    with t.sf() as db:
        cid = db.scalars(select(Category)).first().id
        db.add(Product(id=1, canonical_key="gopro|hero-12", brand="GoPro", model="Hero 12",
                       display_name="GoPro Hero 12", confidence="HIGH", first_seen_at=NOW, last_seen_at=NOW,
                       extractor_version=2))  # fmt: skip
        db.commit()
    return t, cid


def add(t, cid, i, views, bucket, days_old=2.0, at=NOW, product=None, price=30000, src="card"):
    with t.sf() as db:
        if db.get(Ad, str(i)) is None:
            posted = at - timedelta(days=days_old)
            db.add(Ad(id=str(i), category_id=cid, title="t", url_path=f"/m/x/t_{i}", price=price, posted_at=posted,
                      posted_src=src, first_seen_at=at, last_seen_at=at, product_id=product))  # fmt: skip
        db.add(CardObs(ad_id=str(i), at=at, views=views, bucket=bucket))
        db.commit()


def test_baseline_needs_n_and_ignores_selected_buckets(tmp_path):
    t, cid = setup_cat(tmp_path)
    for i in range(7):
        add(t, cid, i, views=20 * (i + 1), bucket="baseline")  # vpd 10..70 при возрасте 2 дн
    add(t, cid, 50, views=10000, bucket="backfill")  # находка (vpd ≥ 50 по отбору) — в норму не входит
    add(t, cid, 51, views=8000, bucket="recheck")
    with t.sf() as db:
        b = baseline(db, cid)
    assert (b.n, b.usable, b.confidence) == (7, False, "NONE")
    add(t, cid, 7, views=160, bucket="report")  # нейтральный замер старого /report тоже годится
    with t.sf() as db:
        b = baseline(db, cid)
    assert (b.n, b.usable, b.median, b.p75, b.days, b.confidence) == (8, True, 45.0, 60, 1, "LOW")


def test_one_listing_is_one_observation(tmp_path):
    t, cid = setup_cat(tmp_path)
    add(t, cid, 1, views=100, bucket="baseline", at=NOW - D)
    add(t, cid, 1, views=300, bucket="followup", at=NOW)  # повторный замер того же объявления
    with t.sf() as db:
        obs = latest_obs(db, ("baseline", "followup"))
    assert list(obs) == ["1"] and obs["1"].views == 300  # последний замер, не два наблюдения


def base_of(t, cid, n=10, views=60):
    for i in range(100, 100 + n):
        add(t, cid, i, views=views, bucket="baseline", at=NOW - D * (i % 2))  # vpd 30, два дня
    with t.sf() as db:
        return baseline(db, cid)


def test_attention_statuses(tmp_path):
    t, cid = setup_cat(tmp_path)
    with t.sf() as db:
        assert product_attention(db, 1, None).status == "NO_OBS"
    add(t, cid, 1, views=400, bucket="candidate", product=1)  # vpd 200
    with t.sf() as db:
        pending = product_attention(db, 1, baseline(db, cid))
    assert (pending.status, pending.multiple, pending.vpd_median) == ("BASELINE_PENDING", None, 200)
    b = base_of(t, cid)
    assert (b.median, b.confidence) == (30, "MEDIUM")
    with t.sf() as db:
        single = product_attention(db, 1, b)
    assert (single.status, single.confidence, single.multiple) == ("ATTENTION_ANOMALY", "LOW", 6.67)  # одна карточка
    add(t, cid, 2, views=200, bucket="followup", product=1)  # vpd 100
    with t.sf() as db:
        measured = product_attention(db, 1, b)
    assert (measured.status, measured.obs_n, measured.vpd_median, measured.multiple, measured.confidence) == (
        "MEASURED", 2, 150, 5.0, "MEDIUM"
    )  # fmt: skip


def test_single_weak_card_is_single_not_anomaly(tmp_path):
    t, cid = setup_cat(tmp_path)
    b = base_of(t, cid)
    add(t, cid, 1, views=40, bucket="candidate", product=1)  # vpd 20 < нормы
    with t.sf() as db:
        a = product_attention(db, 1, b)
    assert (a.status, a.multiple) == ("SINGLE", 0.67)


def test_baseline_is_not_built_from_candidate_cards(tmp_path):
    t, cid = setup_cat(tmp_path)
    for i in range(10):
        add(t, cid, i, views=2000, bucket="candidate", product=1)  # сильные кандидаты
    with t.sf() as db:
        assert baseline(db, cid).n == 0


def test_main_category(tmp_path):
    t, cid = setup_cat(tmp_path)
    add(t, cid, 1, views=1, bucket="candidate", product=1)
    with t.sf() as db:
        assert main_category(db, 1) == cid and main_category(db, 999) is None
