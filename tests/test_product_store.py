"""ADR-022: хранение товаров — назначение при записи выдачи, журнал смен, офлайн-пересборка, диагностика."""

from datetime import timedelta

from sqlalchemy import select

from app.db import Ad, Category, Product, ProductAssignment
from app.models import Listing
from app.services.history import record_search
from app.services.product_store import diagnostics, rebuild
from app.services.products import PRODUCT_EXTRACTOR_VERSION
from tests.test_market import NOW, life, rows


def lst(i, title, price=80000, city="moskva"):
    return Listing(external_id=str(i), title=title, price=price, url=f"https://www.avito.ru/{city}/x/t_{i}",
                   published_at=NOW - timedelta(days=1))  # fmt: skip


def cats(t):
    with t.sf() as db:
        return [c.id for c in db.scalars(select(Category))]


def test_same_model_different_sellers_one_product(tmp_path):
    t = life(tmp_path, {"A": [900]})
    (cid,) = cats(t)
    with t.sf() as db:
        record_search(db, cid, [lst(1, "Pioneer DDJ-FLX4"), lst(2, "Контроллер Pioneer DJ DDJ FLX4 новый", city="spb"),
                                lst(3, "Pioneer DDJ-FLX10"), lst(4, "Stone Island куртка")], set(), NOW)  # fmt: skip
        db.commit()
    ads = {a.id: a for a in rows(t, Ad)}
    assert ads["1"].product_id == ads["2"].product_id is not None
    assert ads["3"].product_id not in (None, ads["1"].product_id)
    assert (ads["4"].product_id, ads["4"].identity_conf, ads["4"].identity_brand) == (None, "LOW", "Stone Island")
    assert all(a.extractor_version == PRODUCT_EXTRACTOR_VERSION for a in ads.values())
    products = {p.canonical_key: p for p in rows(t, Product)}
    assert set(products) == {"pioneer|ddj-flx4", "pioneer|ddj-flx10"}
    assert products["pioneer|ddj-flx4"].display_name == "Pioneer DDJ-FLX4"
    assert len(rows(t, ProductAssignment)) == 3  # LOW без товара в журнал не пишется


def test_query_sighting_is_not_a_second_listing(tmp_path):
    t = life(tmp_path, {"A": [900, 900]})
    real, query = cats(t)
    with t.sf() as db:
        q = db.get(Category, query)
        q.kind, q.scope = "query", "QUERY"
        db.commit()
    for cid, is_q in ((query, True), (real, False), (query, True)):
        with t.sf() as db:
            record_search(db, cid, [lst(1, "Pioneer DDJ-FLX4")], set(), NOW, query=is_q)
            db.commit()
    with t.sf() as db:
        d = diagnostics(db, scopes=("CORE", "QUERY"))
    assert d["ads"] == 1 and d["top"] == [("Pioneer DDJ-FLX4", 1)]  # одно объявление = один сигнал


def test_rebuild_offline_reassigns_and_logs(tmp_path):
    t = life(tmp_path, {"A": [900]})
    (cid,) = cats(t)
    with t.sf() as db:  # объявления «до Phase 2»: без товара
        for i, title in enumerate(["Aimiko U2 Pro 48V", "Aimiko U2 Pro", "Aimiko U2", "Электровелосипед"], 1):
            db.add(Ad(id=str(i), category_id=cid, title=title, url_path="/p", first_seen_at=NOW, last_seen_at=NOW,
                      price=90000))  # fmt: skip
        db.add(Product(canonical_key="old|key", brand="x", model="y", display_name="x y", confidence="HIGH",
                       first_seen_at=NOW, last_seen_at=NOW, extractor_version=0))  # fmt: skip
        db.commit()
    t.provider.calls.clear()
    with t.sf() as db:
        report = rebuild(db, NOW)
    assert t.provider.calls == []  # ни одной загрузки Avito
    assert report == {"ads": 4, "changed": 3, "orphans_removed": 1, "stale_candidates_removed": 0}
    keys = {a.id: (a.product_id, a.identity_conf) for a in rows(t, Ad)}
    assert keys["1"][0] == keys["2"][0] != keys["3"][0] and keys["4"] == (None, "UNKNOWN")
    with t.sf() as db:
        again = rebuild(db, NOW)
    assert again["changed"] == 0 and len(rows(t, ProductAssignment)) == 3  # повтор ничего не меняет
    with t.sf() as db:  # заголовок поменялся (новая модель) — смена в журнале, история цела
        db.get(Ad, "3").title = "Aimiko U2 Pro"
        db.commit()
        rebuild(db, NOW + timedelta(days=1))
    log = [(r.ad_id, r.product_id) for r in rows(t, ProductAssignment) if r.ad_id == "3"]
    assert len(log) == 2 and log[-1][1] == keys["1"][0]


def test_diagnostics_shares_clusters_and_suspicious(tmp_path):
    t = life(tmp_path, {"A": [900]})
    (cid,) = cats(t)
    titles = [("Kugoo Kirin V3 Pro", 30000)] * 3 + [("Kugoo Kirin V3 Pro", 400000)] * 3  # цена ×13 — подозрительно
    titles += [(f"Kugoo Kirin V3 Pro {tail}", 61000) for tail in ("X1 A7", "A7 Q9", "Q9 X1")]  # разные «хвосты»
    titles += [("Электровелосипед", 30000), ("Kugoo б/у", 30000)]
    with t.sf() as db:
        record_search(db, cid, [lst(i, ti, int(p)) for i, (ti, p) in enumerate(titles, 1)], set(), NOW)
        db.commit()
        d = diagnostics(db, scopes=("CORE",))
    assert d["ads"] == 11 and d["products"] == 1  # «A7», «Q9» после Pro — не модели
    assert d["top"] == [("Kugoo Kirin V3 Pro", 9)]
    assert d["share"] == {"HIGH": 81.8, "MEDIUM": 0.0, "LOW": 9.1, "UNKNOWN": 9.1}
    (name, n, flags), = d["suspicious"]
    assert name == "Kugoo Kirin V3 Pro" and any("разброс цены" in f for f in flags)
    assert any("разные коды" in f for f in flags)
    assert d["brand_only"] == [("Kugoo", 1)]


def test_rebuild_script_imports_no_browser():
    """Офлайн по построению: модуль пересборки не тянет Playwright и провайдер браузера."""
    import subprocess
    import sys

    banned = "('playwright', 'app.providers.avito_browser')"
    code = f"import sys, scripts.rebuild_products; print(any(m.startswith({banned}) for m in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
