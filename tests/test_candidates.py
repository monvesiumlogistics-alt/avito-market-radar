"""ADR-023: Product Candidate Engine — дешёвые сигналы выдачи → кандидаты для карточек Phase 4. 0 запросов Avito."""

from datetime import timedelta
from types import SimpleNamespace as NS

from sqlalchemy import select

from app.db import Ad, AdQuerySighting, Category
from app.models import Listing
from app.services.candidates import AdRow, evaluate, independence, load_rows, product_metrics, run
from app.services.history import record_search
from tests.test_market import NOW, life

D = timedelta(days=1)
PRODUCT = NS(display_name="Pioneer DDJ-FLX4", confidence="HIGH")


def row(i, city="moskva", shop=None, days_ago=1.0, promoted=False, conf="MEDIUM", cat=1, price=40000, pid=1):
    at = NOW - timedelta(days=days_ago)
    return AdRow(str(i), pid, cat, frozenset({"CORE"}), price, city, shop, promoted, at, conf, NOW, True)


def metrics(rows, product=PRODUCT, data_since=None, cats=None):
    cats = cats or {1: NS(name="DJ")}
    return product_metrics(1, rows, product, cats, NOW, 0, data_since or {})


# --- REPEATED и независимость ---


def test_three_listings_of_one_model_are_repeated():
    m = metrics([row(1, "moskva", days_ago=1), row(2, "spb", days_ago=2), row(3, "kazan", days_ago=4)])
    cand, reject = evaluate(m, hist_days=10, category_p75=None)
    assert reject is None and "REPEATED" in cand.reasons and "MULTI_CITY" in cand.reasons
    assert cand.independence == "HIGH"


def test_one_shop_one_city_one_day_is_low_independence():
    mass = metrics([row(i, "moskva", shop="Магазин", days_ago=1) for i in range(5)])
    spread = metrics([row(i, c, days_ago=d) for i, (c, d) in enumerate([("moskva", 1), ("spb", 2), ("kazan", 3),
                                                                      ("ufa", 4), ("omsk", 5)])])  # fmt: skip
    low, high = evaluate(mass, 10, None)[0], evaluate(spread, 10, None)[0]
    assert (low.independence, high.independence) == ("LOW", "HIGH")
    assert low.confidence == "LOW" and high.confidence == "HIGH"


def test_dominant_shop_is_low_even_across_cities():
    """Реальный случай: Alienware 16 Area 51 — 3 магазина, 2 города, но 75% объявлений у одного."""
    rows = [row(i, c, shop=s) for i, (c, s) in enumerate([("moskva", "A"), ("moskva", "A"), ("moskva", "A"),
                                                         ("spb", "A"), ("moskva", "A"), ("moskva", "A"),
                                                         ("spb", "B"), ("moskva", "C")])]  # fmt: skip
    assert independence(metrics(rows)) == ("LOW", "75% объявлений у одного магазина")


def test_promoted_listings_do_not_make_strong_signal():
    m = metrics([row(1), row(2, "spb", promoted=True), row(3, "kazan", promoted=True)])
    assert evaluate(m, 10, None) == (None, "mostly_promoted")
    assert (m.organic_14d, m.promoted_14d) == (1, 2)


def test_single_expensive_listing_is_not_a_candidate():
    m = metrics([row(1, price=900000, days_ago=0.5)], data_since={1: NOW - 20 * D})
    assert evaluate(m, 30, category_p75=50000) == (None, "single_listing")


# --- EMERGING и даты ---


def test_two_fresh_listings_can_be_emerging_with_prior_coverage():
    rows = [row(1, "moskva", days_ago=0.5), row(2, "spb", days_ago=1.5)]
    cand, _ = evaluate(metrics(rows, data_since={1: NOW - 10 * D}), hist_days=10, category_p75=None)
    assert cand and "EMERGING" in cand.reasons and "REPEATED" not in cand.reasons


def test_first_crawl_does_not_make_everything_new():
    """Первый обход: категорию видим всего 1 день — «новизну» товара доказать нельзя."""
    rows = [row(1, "moskva", days_ago=0.5), row(2, "spb", days_ago=1.0)]
    assert evaluate(metrics(rows, data_since={1: NOW - 1 * D}), 1, None) == (None, "insufficient_prior_coverage")


def test_old_listings_first_seen_by_bot_are_not_a_trend():
    old = [row(i, c, days_ago=20) for i, c in enumerate(["moskva", "spb", "kazan"])]  # дата рынка — 20 дн назад
    assert evaluate(metrics(old, data_since={1: NOW - 30 * D}), 30, None) == (None, "only_old_listings")
    unknown_date = [row(1, days_ago=0.2, conf="LOW"), row(2, "spb", days_ago=0.2, conf="LOW")]  # только first_seen
    assert evaluate(metrics(unknown_date, data_since={1: NOW - 30 * D}), 30, None) == (
        None, "insufficient_reliable_date"
    )  # fmt: skip


def test_short_history_gives_no_growth():
    rows = [row(i, f"c{i}", days_ago=0.5 + i * 0.3) for i in range(4)] + [row(9, "x", days_ago=4)]
    young = evaluate(metrics(rows, data_since={1: NOW - 30 * D}), hist_days=3, category_p75=None)[0]
    mature = evaluate(metrics(rows, data_since={1: NOW - 30 * D}), hist_days=10, category_p75=None)[0]
    assert "GROWTH" not in young.reasons and "GROWTH" in mature.reasons


def test_high_ticket_only_strengthens_existing_candidate():
    rows = [row(i, c, price=150000) for i, c in enumerate(["moskva", "spb", "kazan"])]
    assert "HIGH_TICKET" in evaluate(metrics(rows), 10, category_p75=60000)[0].reasons


# --- уверенность модели ≠ уверенность кандидата ---


def test_identity_and_candidate_confidence_are_independent():
    rows = [row(i, c, days_ago=d) for i, (c, d) in enumerate([("moskva", 1), ("spb", 2), ("kazan", 3), ("ufa", 4),
                                                            ("omsk", 5)])]  # fmt: skip
    strong_market = evaluate(metrics(rows, product=NS(display_name="Doona X", confidence="MEDIUM")), 10, None)[0]
    assert (strong_market.metrics.identity, strong_market.independence, strong_market.confidence) == (
        "MEDIUM", "HIGH", "MEDIUM"
    )  # fmt: skip
    weak_market = evaluate(metrics([row(i, "moskva", shop="S") for i in range(3)]), 10, None)[0]
    assert (weak_market.metrics.identity, weak_market.confidence) == ("HIGH", "LOW")
    assert independence(weak_market.metrics)[0] == "LOW"


# --- выборка из БД: dedup, области, LOW/UNKNOWN ---


def lst(i, title, city="moskva"):
    return Listing(external_id=str(i), title=title, price=50000, url=f"https://www.avito.ru/{city}/x/t_{i}",
                   published_at=NOW - D)  # fmt: skip


def seeded(tmp_path, scopes):
    t = life(tmp_path, {"A": [900] * len(scopes)})
    with t.sf() as db:
        cats = db.scalars(select(Category)).all()
        for c, sc in zip(cats, scopes, strict=True):
            c.scope = sc
        db.commit()
        return t, [c.id for c in cats]


def test_one_listing_counted_once_despite_query_sightings(tmp_path):
    t, (cid, q1, q2) = seeded(tmp_path, ["CORE", "QUERY", "QUERY"])
    with t.sf() as db:
        for c in (q1, q2):
            db.get(Category, c).kind = "query"
        db.commit()
        record_search(db, cid, [lst(1, "Pioneer DDJ-FLX4")], set(), NOW)
        for q in (q1, q2):
            record_search(db, q, [lst(1, "Pioneer DDJ-FLX4")], set(), NOW, query=True)
        db.commit()
        rows, _, _, hits = load_rows(db)
        assert len(db.scalars(select(AdQuerySighting)).all()) == 2
    assert len(rows) == 1 and rows[0].scopes == {"CORE", "QUERY"} and hits == {rows[0].product_id: 2}


def test_scopes_low_identity_and_explore(tmp_path):
    t, (core, explore, off, hard) = seeded(tmp_path, ["CORE", "EXPLORE", "OFF", "HARD_EXCLUDE"])
    with t.sf() as db:
        record_search(db, explore, [lst(i, "Doona X", c) for i, c in ((1, "moskva"), (2, "spb"), (3, "kazan"))],
                      set(), NOW)  # fmt: skip
        record_search(db, off, [lst(i, "Pioneer DDJ-FLX4", c) for i, c in ((4, "moskva"), (5, "spb"), (6, "kzn"))],
                      set(), NOW)  # fmt: skip
        record_search(db, hard, [lst(7, "Pioneer DDJ-FLX4")], set(), NOW)
        record_search(db, core, [lst(i, "Stone Island куртка", c) for i, c in ((8, "a"), (9, "b"), (10, "c"))],
                      set(), NOW)  # fmt: skip
        db.commit()
        result = run(db, NOW)
    names = [c.metrics.name for c in result["candidates"]]
    assert names == ["Doona X"]  # EXPLORE — да; OFF/HARD — не участвуют; бренд без модели (LOW) — не товар
    assert result["eligible_ads"] == 3
    with t.sf() as db:
        assert db.get(Ad, "8").product_id is None



# --- хранение и отчёт ---


def test_persist_lifecycle_new_active_cooled(tmp_path):
    from app.db import CandidateLog, ProductCandidate
    from app.services.candidates import persist

    t, (cid,) = seeded(tmp_path, ["CORE"])
    with t.sf() as db:
        record_search(db, cid, [lst(i, "Doona X", c) for i, c in ((1, "moskva"), (2, "spb"), (3, "kazan"))],
                      set(), NOW)  # fmt: skip
        db.commit()
        cands = run(db, NOW)["candidates"]
        assert persist(db, cands, NOW) == {"NEW": 1}
        assert persist(db, cands, NOW + D) == {"ACTIVE": 1}  # на следующий день снова кандидат
        assert persist(db, [], NOW + 2 * D) == {"COOLED": 1}  # выпал
        assert persist(db, cands, NOW + 3 * D) == {"ACTIVE": 1}  # вернулся
        (r,) = db.scalars(select(ProductCandidate)).all()
        assert (r.first_candidate_at, r.last_candidate_at) == (NOW, NOW + 3 * D)
        assert "REPEATED" in r.reason_codes and r.metrics["listings_14d"] == 3
        assert len(db.scalars(select(CandidateLog)).all()) == 3  # дни, когда был кандидатом


def test_candidate_report_makes_no_network_requests():
    import subprocess
    import sys

    banned = "('playwright', 'app.providers.avito_browser')"
    code = f"import sys, scripts.product_candidates; print(any(m.startswith({banned}) for m in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "False"
