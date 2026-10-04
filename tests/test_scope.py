"""ADR-020: область обхода — каждая категория карты ровно в одной области; обход по графику области;
брендовые запросы не «присваивают» объявления настоящих категорий."""

import csv
import sqlite3
from collections import Counter
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db import Ad, AdQuerySighting, Category, init_db
from app.scope import ACTIVE, EXTRA_QUERIES, QUERY_TERMS, SCOPE, SCOPES
from app.services.history import record_search
from app.services.scoping import apply_scope, expected_search_loads, key_of, query_matcher
from tests.test_history import listing
from tests.test_market import NOW, life, rows, search_html, url_of

CATALOG = Path("data/catalog.csv")  # runtime-карта Avito (не в git)
MANIFEST = Path(__file__).parent / "fixtures" / "catalog_keys.csv"  # ключи той же карты, в git (чистый клон)

REAL = {k: v for k, v in SCOPE.items() if "?q=" not in k}
QUERIES = {k: v for k, v in SCOPE.items() if "?q=" in k}


# --- полнота и однозначность ---


def test_every_map_category_in_exactly_one_scope():
    assert len(REAL) == 209  # dict: один ключ = одна область, двойного членства быть не может
    assert all(v[0] in SCOPES for v in SCOPE.values())
    assert Counter(v[0] for v in REAL.values()) == {
        "CORE": 25, "WATCH": 18, "EXPLORE": 54, "OFF": 101, "HARD_EXCLUDE": 11,
    }  # fmt: skip
    dups = {k: v[3] for k, v in SCOPE.items() if v[3]}
    assert len(dups) == 1 and all(d in REAL for d in dups.values())  # «Игровые приставки» (дубль) → OFF, учтён явно
    assert all(SCOPE[k][0] == "OFF" for k in dups)


def test_query_watch_scope():
    q = Counter(v[0] for v in QUERIES.values())
    assert q == {"QUERY": 16, "OFF": 32}
    assert {v[1] for v in EXTRA_QUERIES.values()} == {"DJI Mini", "DJI Avata"}
    assert not set(EXTRA_QUERIES) & set(SCOPE)


def test_scope_equals_catalog_manifest_clean_clone():
    with open(MANIFEST, encoding="utf-8", newline="") as f:
        keys = [r["key"] for r in csv.DictReader(f, delimiter=";")]
    assert len(keys) == len(set(keys)) == 209
    assert set(keys) == set(REAL)  # 209/209: ни потерянных, ни лишних, ни неявных


@pytest.mark.skipif(not CATALOG.exists(), reason="карта Avito лежит локально (data/ не в git)")
def test_scope_covers_real_catalog_map():
    with open(CATALOG, encoding="utf-8-sig", newline="") as f:
        keys = {r["key"] for r in csv.DictReader(f, delimiter=";") if r.get("key", "").startswith("/")}
    assert keys == set(REAL)  # ни потерянных, ни лишних


# --- применение к БД ---


def test_apply_scope_sets_marks_unknown_and_creates_extra(tmp_path):
    sf = init_db(f"sqlite:///{tmp_path}/t.db")
    core_key = next(k for k, v in REAL.items() if v[0] == "CORE")
    dup_key = next(k for k, v in SCOPE.items() if v[3])
    q_key = next(k for k, v in QUERIES.items() if v[0] == "QUERY")
    with sf() as db:
        for key in (core_key, dup_key, q_key, "/rossiya/new/unknown-X"):
            db.add(Category(section="s", name=key[-10:], url="https://www.avito.ru" + key, discovered_at=NOW))
        db.commit()
    report = apply_scope(sf, NOW)
    assert report["unknown"] == ["/rossiya/new/unknown-X"] and report["created"] == 2
    with sf() as db:
        cats = {key_of(c.url): c for c in db.scalars(select(Category))}
    core = cats[core_key]
    assert (core.scope, core.kind, core.scope_status) == ("CORE", "category", "hypothesis")
    assert cats[dup_key].scope == "OFF" and cats[dup_key].duplicate_of == SCOPE[dup_key][3]
    assert (cats[q_key].scope, cats[q_key].kind) == ("QUERY", "query")
    assert cats["/rossiya/new/unknown-X"].scope == "OFF"  # неизвестное не обходится, пока не внесено в scope.py
    assert cats["/rossiya?q=dji+mini"].kind == "query" and cats["/rossiya?q=dji+mini"].scope == "QUERY"
    assert apply_scope(sf, NOW)["created"] == 0  # идемпотентно


def test_expected_search_loads():
    s = Settings(_env_file=None)
    plan = expected_search_loads({"CORE": 25, "WATCH": 18, "QUERY": 18, "EXPLORE": 54, "OFF": 133}, s)
    assert plan == {"CORE": 35.0, "WATCH": 9.8, "QUERY": 9.8, "EXPLORE": 5.4, "total": 60.0}


# --- обход по графику области ---


def scoped(t, scopes: dict[str, str]):
    with t.sf() as db:
        for c in db.scalars(select(Category)):
            c.scope = scopes[c.name]
        db.commit()


async def test_sweep_follows_scope_cadence(tmp_path):
    t = life(tmp_path, {"A": [900] * 5})
    scoped(t, {"A 1": "CORE", "A 2": "WATCH", "A 3": "EXPLORE", "A 4": "OFF", "A 5": "HARD_EXCLUDE"})
    t.crawler.start("sweep")
    await t.crawler._task
    swept = {u.split("/")[-1].split("-H")[0] for u in t.provider.calls}
    assert swept == {"c1", "c2", "c3"}  # OFF / HARD — 0 запросов
    assert sum("c2-H" in u for u in t.provider.calls) == 1  # WATCH — только 1 страница

    t.now[0] += timedelta(days=1)
    t.provider.calls.clear()
    t.crawler.start("sweep")
    await t.crawler._task
    assert {u.split("/")[-1].split("-H")[0] for u in t.provider.calls} <= {"c1"}  # WATCH/EXPLORE ещё не пора


async def test_explore_capped_per_run(tmp_path):
    t = life(tmp_path, {"A": [900] * 4}, explore_per_run=2)
    scoped(t, {f"A {i}": "EXPLORE" for i in range(1, 5)})
    t.crawler.start("sweep")
    await t.crawler._task
    assert len({u.split("-H")[0] for u in t.provider.calls}) == 2


async def test_report_skips_explore_and_off(tmp_path):
    t = life(tmp_path, {"A": [900, 900, 900]})
    scoped(t, {"A 1": "CORE", "A 2": "EXPLORE", "A 3": "OFF"})
    t.crawler.start()
    await t.crawler._task
    assert not any("c2-H" in u or "c3-H" in u for u in t.provider.calls)


# --- запросы: sightings отдельно от канонической категории (ADR-021) ---


def two_cats(t):
    with t.sf() as db:
        real, query = db.scalars(select(Category)).all()
        query.kind, query.name = "query", "Prada — муж."
        db.commit()
        return real.id, query.id


def sightings(t):
    return sorted((x.ad_id, x.query_category_id) for x in rows(t, AdQuerySighting))


def test_query_only_ad_has_no_fake_category_then_real_becomes_canonical(tmp_path):
    t = life(tmp_path, {"A": [900, 900]})
    real_id, query_id = two_cats(t)
    with t.sf() as db:
        assert record_search(db, query_id, [listing(1)], set(), NOW, query=True) == (1, 0)
        db.commit()
    assert rows(t, Ad)[0].category_id is None  # запрос — не категория
    with t.sf() as db:  # появилось в настоящей категории: для неё новое и теперь её
        assert record_search(db, real_id, [listing(1)], set(), NOW) == (1, 0)
        db.commit()
    with t.sf() as db:  # снова в запросе — знакомое, категория не меняется
        assert record_search(db, query_id, [listing(1)], set(), NOW, query=True) == (0, 1)
        db.commit()
    assert rows(t, Ad)[0].category_id == real_id and sightings(t) == [("1", query_id)]


def test_one_ad_many_query_sightings(tmp_path):
    t = life(tmp_path, {"A": [900, 900, 900]})
    with t.sf() as db:
        ids = [c.id for c in db.scalars(select(Category))]
        for c in db.scalars(select(Category)):
            c.kind = "query"
        db.commit()
    for qid in ids:
        with t.sf() as db:
            record_search(db, qid, [listing(1), listing(1)], set(), NOW, query=True)  # и дубль на странице
            db.commit()
    assert sightings(t) == [("1", i) for i in sorted(ids)] and len(rows(t, Ad)) == 1  # dedup по external_id


@pytest.mark.parametrize(
    ("query", "title", "ok"),
    [
        ("Prada — муж.", "Supreme Arc Thermal Lined Zip Up Hooded Sweatshirt", False),  # утечка поиска Avito
        ("Prada — муж.", "Куртка PRADA Linea Rossa", True),
        ("Prada — муж.", "Кроссовки Прада America's Cup", True),
        ("Arcteryx — муж.", "Куртка Arc'teryx Beta LT", True),
        ("Arcteryx — муж.", "ARC’TERYX Atom hoody", True),
        ("CP Company — муж.", "Худи C.P. Company goggle", True),
        ("CP Company — муж.", "C.P.Company овершот", True),
        ("Maison Margiela — муж.", "Кеды Margiela Replica", True),
        ("Fear of God Essentials — муж.", "Худи Essentials FOG", True),
        ("Stone Island — муж.", "Худи Stone Island shadow", True),
        ("Stone Island — муж.", "Кулон камень Island", False),
        ("Louis Vuitton — муж.", "Сумка LV Keepall", True),
        ("DJI Mini", "Квадрокоптер DJI Mini 4 Pro fly more", True),
        ("DJI Mini", "Mini 4 Pro", False),
        ("DJI Avata", "DJI Avata 2 + очки Goggles 3", True),
        ("DJI Avata", "DJI Mini 3", False),
        ("Неизвестный — муж.", "что угодно", False),  # нет правил — fail closed
    ],
)
def test_query_match_validation(query, title, ok):
    assert query_matcher(query)(title) is ok


def test_every_query_watch_has_match_terms():
    names = [v[1] for v in QUERIES.values() if v[0] == "QUERY"] + [v[1] for v in EXTRA_QUERIES.values()]
    assert all(n.split(" — ")[0] in QUERY_TERMS for n in names)


async def test_query_leak_not_recorded_by_crawl(tmp_path):
    t = life(tmp_path, {"A": [900, 900]})
    _, query_id = two_cats(t)
    t.pages[url_of("Ac2")] = search_html(
        ("101", "Куртка Prada", 20000, "1 день назад"), ("102", "Supreme box logo", 20000, "1 день назад")
    )
    with t.sf() as db:
        db.get(Category, query_id).scope = "QUERY"
        db.commit()
    t.crawler.start("sweep")
    await t.crawler._task
    assert sightings(t) == [("101", query_id)] and "102" not in {a.id for a in rows(t, Ad)}  # утечка не записана


# --- fail closed, CORE-гипотеза ежедневно, миграция ---


def test_unknown_category_defaults_off(tmp_path):
    sf = init_db(f"sqlite:///{tmp_path}/t.db")
    with sf() as db:
        db.add(Category(section="s", name="новая", url="u", discovered_at=NOW))
        db.commit()
        assert db.scalars(select(Category)).one().scope == "OFF"


async def test_core_hypothesis_not_quiet_skipped_confirmed_is(tmp_path):
    t = life(tmp_path, {"A": [900, 900]})
    with t.sf() as db:
        a1, a2 = db.scalars(select(Category)).all()
        a1.scope_status, a2.scope_status = "hypothesis", "confirmed"
        db.commit()
    t.crawler.start("sweep")
    await t.crawler._task  # по 1 объявлению за неделю — обе «тихие»
    t.now[0] += timedelta(hours=20)
    t.provider.calls.clear()
    t.crawler.start("sweep")
    await t.crawler._task
    swept = {u.split("/")[-1].split("-H")[0] for u in t.provider.calls}
    assert swept == {"c1"}  # гипотеза — ежедневно, подтверждённая тихая — через день


def test_migration_old_ads_schema(tmp_path):
    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE ads (id VARCHAR(64) PRIMARY KEY, category_id INTEGER NOT NULL, title VARCHAR(500) NOT NULL,"
        " model_key VARCHAR(200), price INTEGER, url_path VARCHAR(500) NOT NULL, city VARCHAR(300), shop VARCHAR(300),"
        " seller_url VARCHAR(500), image_url VARCHAR(1000), posted_at DATETIME, posted_src VARCHAR(8),"
        " first_seen_at DATETIME NOT NULL, last_seen_at DATETIME NOT NULL, promoted_seen INTEGER NOT NULL,"
        " status VARCHAR(16) NOT NULL, status_at DATETIME, query_id INTEGER);"
        "CREATE INDEX ix_ads_model_key ON ads (model_key);"
        "INSERT INTO ads VALUES ('1', 5, 't', 'm', 1, '/p', NULL, NULL, NULL, NULL, NULL, NULL,"
        " '2026-10-01 00:00:00', '2026-10-02 00:00:00', 0, 'live', NULL, 9);"
    )
    con.close()
    sf = init_db(f"sqlite:///{path}")
    init_db(f"sqlite:///{path}")  # повторно — без изменений
    con = sqlite3.connect(path)
    cols = {r[1]: r[3] for r in con.execute("pragma table_info(ads)")}
    assert cols["category_id"] == 0 and "query_id" not in cols
    assert con.execute("select ad_id, query_category_id from ad_query_sightings").fetchall() == [("1", 9)]
    con.close()
    with sf() as db:
        assert db.get(Ad, "1").model_key == "m"


def test_apply_scope_releases_query_owned_ads(tmp_path):
    t = life(tmp_path, {"A": [900]})
    with t.sf() as db:
        q = Category(section="s", name="Prada — муж.", url="https://www.avito.ru/x?q=prada", discovered_at=NOW,
                     kind="query")  # fmt: skip
        db.add(q)
        db.flush()
        db.add(Ad(id="7", category_id=q.id, title="t", url_path="/p", first_seen_at=NOW, last_seen_at=NOW))
        db.commit()
        qid = q.id
    apply_scope(t.sf, NOW)
    assert rows(t, Ad)[0].category_id is None and sightings(t) == [("7", qid)]


def test_active_scopes():
    assert ACTIVE == ("CORE", "WATCH", "QUERY", "EXPLORE")
