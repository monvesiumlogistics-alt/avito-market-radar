import sqlite3
from datetime import datetime

from sqlalchemy import select

from app.db import Category, init_db
from scripts.import_map import import_map

CSV = (
    "﻿parent;name;score;ok;count;vpd;today;p25;p50;p75;newPct;key\n"
    "Ноутбуки;Ноутбуки Alienware;109.2;True;179;53;7;25850;48990;66000;4;/rossiya/noutbuki/alienware-ASgB\n"
    "Музыка;Аккордеоны;103.1;True;17879;105;63;4990;15000;36000;14;/rossiya/muzykalnye_instrumenty/akk-ASgC\n"
    "Пусто;Без ключа;1;True;1;1;1;1;1;1;1;\n"
)


def test_import_map_upsert_idempotent(tmp_path):
    f = tmp_path / "catalog.csv"
    f.write_text(CSV, encoding="utf-8")  # BOM внутри текста, как у utf-8-sig файла
    sf = init_db(f"sqlite:///{tmp_path}/t.db")
    now = datetime(2026, 10, 3, 12)
    assert import_map(sf, f, now) == 2
    with sf() as db:
        db.scalars(select(Category).where(Category.name == "Аккордеоны")).one().last_best_vpd = 77
        db.commit()
    assert import_map(sf, f, now) == 2  # повтор: без дублей, история обходов цела
    with sf() as db:
        cats = {c.name: c for c in db.scalars(select(Category))}
    assert len(cats) == 2
    a = cats["Ноутбуки Alienware"]
    assert (a.section, a.url, a.prior_score, a.discovered_at) == (
        "noutbuki",
        "https://www.avito.ru/rossiya/noutbuki/alienware-ASgB",
        109.2,
        now,
    )
    assert cats["Аккордеоны"].last_best_vpd == 77 and cats["Аккордеоны"].last_crawled_at is None


def test_prior_score_column_added_to_existing_table(tmp_path):
    db_file = tmp_path / "old.db"
    con = sqlite3.connect(db_file)  # БД старой схемы: categories без prior_score
    con.execute(
        "CREATE TABLE categories (id INTEGER PRIMARY KEY, section VARCHAR(64), name VARCHAR(200), "
        "url VARCHAR(300) UNIQUE, discovered_at DATETIME, last_crawled_at DATETIME, last_run_id INTEGER, "
        "last_best_vpd INTEGER, last_status VARCHAR(16), last_days_covered FLOAT)"
    )
    con.execute("INSERT INTO categories (section, name, url, discovered_at) VALUES ('s', 'n', 'u', '2026-10-01')")
    con.commit()
    con.close()
    for _ in range(2):  # идемпотентно
        sf = init_db(f"sqlite:///{db_file}")
    with sf() as db:
        (c,) = db.scalars(select(Category)).all()
    assert c.name == "n" and c.prior_score is None
