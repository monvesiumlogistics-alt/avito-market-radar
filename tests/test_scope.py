"""ADR-020: область обхода — каждая категория карты ровно в одной области; обход по графику области;
брендовые запросы не «присваивают» объявления настоящих категорий."""

import csv
from collections import Counter
from datetime import timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from app.config import Settings
from app.db import Ad, Category, init_db
from app.scope import ACTIVE, EXTRA_QUERIES, SCOPE, SCOPES
from app.services.history import record_search
from app.services.scoping import apply_scope, expected_search_loads, key_of
from tests.test_history import listing
from tests.test_market import NOW, life, rows

CATALOG = Path("data/catalog.csv")  # карта Avito (не в git): проверяется, если лежит локально

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


# --- запросы не присваивают объявления ---


def test_query_sighting_does_not_steal_category_ownership(tmp_path):
    t = life(tmp_path, {"A": [900, 900]})
    with t.sf() as db:
        real, query = db.scalars(select(Category)).all()
        query.kind = "query"
        db.commit()
        real_id, query_id = real.id, query.id
    with t.sf() as db:
        assert record_search(db, query_id, [listing(1)], set(), NOW, query=True) == (1, 0)
        db.commit()
    with t.sf() as db:  # то же объявление в настоящей категории: для неё оно новое и теперь её
        assert record_search(db, real_id, [listing(1)], set(), NOW) == (1, 0)
        db.commit()
    with t.sf() as db:  # и снова в запросе — уже знакомое, категория не меняется
        assert record_search(db, query_id, [listing(1)], set(), NOW, query=True) == (0, 1)
        db.commit()
    (ad,) = rows(t, Ad)
    assert (ad.category_id, ad.query_id) == (real_id, query_id)


def test_active_scopes():
    assert ACTIVE == ("CORE", "WATCH", "QUERY", "EXPLORE")
