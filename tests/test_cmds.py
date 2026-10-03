"""F2-F5: /top, /export, /cats, /skip, /unskip, кнопки 👍/👎 (ADR-008)."""

import csv
import io
from datetime import datetime, timedelta
from types import SimpleNamespace

from aiogram import Bot, Dispatcher
from aiogram.types import CallbackQuery, Chat, Message, Update, User
from sqlalchemy import select, text

from app.db import Category, CrawlRun, Find, init_db
from app.services.market_cmds import cats_text, export_csv, set_feedback, set_skipped, top_finds, top_messages
from app.services.market_logic import crawl_order, feedback_factor
from app.telegram.handlers import build_router
from tests.test_handlers import ADMIN, FakeCrawler, FakeSession, send
from tests.test_market import NOW, card_url, item_html, life, runs, search_html, summary_text, url_of
from tests.test_market_logic import cat as logic_cat


def seed(tmp_path):
    sf = init_db(f"sqlite:///{tmp_path}/c.db")
    with sf() as db:
        db.add_all(
            [
                Category(section="muzykalnye_instrumenty", name="Аккордеоны", url="u1", discovered_at=NOW),
                Category(section="muzykalnye_instrumenty", name="Гитары", url="u2", discovered_at=NOW),
                Category(section="velosipedy", name="Шоссейные", url="u3", discovered_at=NOW),
                CrawlRun(started_at=NOW),
            ]
        )
        db.commit()
    return sf


def add_find(sf, title, vpd, days_ago=1, key=None, cat_id=1, **kw):
    with sf() as db:
        f = Find(
            run_id=1, category_id=cat_id, group_key=key or title.lower(), title=title, price_min=20000,
            price_max=25000, vpd=vpd, today=5, age_days=2.0, date_checked=True, copies=2, url="https://www.avito.ru/x_1",
            external_id=title, hot=vpd >= 100, created_at=NOW - timedelta(days=days_ago), **kw,
        )  # fmt: skip
        db.add(f)
        db.commit()
        return f.id


def make(sf):
    session = FakeSession()
    bot = Bot("42:TEST", session=session)
    dp = Dispatcher()
    dp.include_router(
        build_router(ADMIN, SimpleNamespace(paused=False, blocked=False, busy=False), sf, None, FakeCrawler())
    )
    return SimpleNamespace(bot=bot, dp=dp, session=session)


# --- F2 /top ---


def test_top_dedupe_days_sorted_limit(tmp_path):
    sf = seed(tmp_path)
    add_find(sf, "Баян Roland", 80, key="roland")
    add_find(sf, "Баян Roland 2", 150, key="roland")  # лучшая в группе
    add_find(sf, "Old Yamaha", 500, days_ago=10)
    for i in range(20):
        add_find(sf, f"Item {i}", 60 + i)
    with sf() as db:
        res = top_finds(db, 7, NOW)
        assert len(res) == 15 and res[0].title == "Баян Roland 2" or res[0].vpd == 150
        assert [f.vpd for f in res] == sorted((f.vpd for f in res), reverse=True)
        assert sum(f.group_key == "roland" for f in res) == 1 and all(f.title != "Old Yamaha" for f in res)
        assert len(top_finds(db, 30, NOW)) == 15 and top_finds(db, 30, NOW)[0].title == "Old Yamaha"
        assert top_messages(db, 7, NOW)[0].startswith("<b>📊 Топ находок · 7 дн</b>")


def test_top_empty(tmp_path):
    with seed(tmp_path)() as db:
        assert top_messages(db, 7, NOW) == ["За 7 дн находок нет."]


async def test_top_handler_days_arg(tmp_path):
    sf = seed(tmp_path)
    add_find(sf, "Pioneer DDJ-400", 120, days_ago=0)
    t = make(sf)
    await send(t, "/top 3")
    assert "Топ находок · 3 дн" in t.session.requests[-1].text and "Pioneer DDJ-400" in t.session.requests[-1].text
    await send(t, "/top abc")
    assert "Топ находок · 7 дн" in t.session.requests[-1].text


# --- F3 /export ---


def test_export_csv_columns_bom_and_formula_guard(tmp_path):
    sf = seed(tmp_path)
    add_find(sf, "=HYPERLINK(1) Pioneer DDJ-400", 120)
    add_find(sf, "Коляска", 90)
    with sf() as db:
        raw = export_csv(db)
    assert raw.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(raw.decode("utf-8-sig")), delimiter=";"))
    assert rows[0] == [
        "date", "section", "category", "title", "price_min", "price_max", "vpd", "today", "age_days", "copies",
        "hot", "url", "goofish_url",
    ]  # fmt: skip
    by_title = {r[3]: r for r in rows[1:]}
    assert "'=HYPERLINK(1) Pioneer DDJ-400" in by_title and by_title["Коляска"][12] == ""
    r = by_title["'=HYPERLINK(1) Pioneer DDJ-400"]
    assert r[1:3] == ["muzykalnye_instrumenty", "Аккордеоны"] and r[10] == "1" and "goofish.com/search?q=" in r[12]


async def test_export_handler_sends_document(tmp_path):
    sf = seed(tmp_path)
    t = make(sf)
    await send(t, "/export")
    assert t.session.requests[-1].text == "Находок пока нет."
    add_find(sf, "Баян", 100)
    await send(t, "/export")
    doc = t.session.requests[-1].document
    assert doc.filename.startswith("finds_") and doc.filename.endswith(".csv") and doc.data.startswith(b"\xef\xbb\xbf")


# --- F4 /cats /skip /unskip ---


def test_cats_skip_unskip(tmp_path):
    sf = seed(tmp_path)
    with sf() as db:
        assert "Музыкальные инструменты" in cats_text(db)[0] and "2 / 0" in cats_text(db)[0]
        assert set_skipped(db, "МУЗЫКАЛ", True) == 2  # регистр не важен, slug и русское имя раздела
        assert set_skipped(db, "гитар", True) == 1 and set_skipped(db, "x", True) == 0  # короче 2 букв — ничего
        assert "2 / 2" in cats_text(db)[0] and "⛔" in cats_text(db)[0]
        assert set_skipped(db, "аккорд", False) == 1
        assert [c.skipped for c in db.scalars(select(Category).order_by(Category.id))] == [False, True, False]


async def test_skip_handlers(tmp_path):
    sf = seed(tmp_path)
    t = make(sf)
    await send(t, "/skip")
    assert t.session.requests[-1].text.startswith("/skip текст")
    await send(t, "/skip velosiped")
    assert t.session.requests[-1].text == "выключено категорий: 1"
    await send(t, "/skip nothing-here")
    assert t.session.requests[-1].text.startswith("Ничего не нашла")
    await send(t, "/cats")
    assert "Велосипеды" in t.session.requests[-1].text and "1 / 1" in t.session.requests[-1].text
    await send(t, "/unskip velosiped")
    assert t.session.requests[-1].text == "возвращено категорий: 1"


def test_crawl_order_excludes_skipped():
    a, b = logic_cat(1, "A", prior=5), logic_cat(2, "B", prior=9)
    b.skipped = True
    assert [c.id for c in crawl_order([a, b], 1, NOW)] == [1]


async def test_crawler_does_not_visit_skipped(tmp_path):
    t = life(tmp_path, {"A": [900, 900]})
    with t.sf() as db:
        db.execute(text("UPDATE categories SET skipped = 1 WHERE name = 'A 2'"))
        db.commit()
    t.crawler.start()
    await t.crawler._task
    assert not any("/c2-H" in u for u in t.provider.calls) and runs(t)[0].status == "done"


def test_old_db_gets_new_columns(tmp_path):
    import sqlite3

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE categories (id INTEGER PRIMARY KEY, section TEXT, name TEXT, url TEXT, discovered_at TEXT)"
    )
    con.execute("CREATE TABLE finds (id INTEGER PRIMARY KEY)")
    con.commit()
    con.close()
    sf = init_db(f"sqlite:///{path}")
    init_db(f"sqlite:///{path}")  # идемпотентно
    with sf() as db:
        cols = {r[1] for r in db.execute(text("PRAGMA table_info(categories)"))}
        fcols = {r[1] for r in db.execute(text("PRAGMA table_info(finds)"))}
    assert {"skipped", "prior_score"} <= cols and "feedback" in fcols


# --- F5 👍/👎 ---


def test_set_feedback(tmp_path):
    sf = seed(tmp_path)
    fid = add_find(sf, "Баян", 100)
    with sf() as db:
        assert set_feedback(db, fid, 1) and db.get(Find, fid).feedback == 1
        assert set_feedback(db, fid, -1) and db.get(Find, fid).feedback == -1  # перезапись, не накопление
        assert not set_feedback(db, 999, 1) and not set_feedback(db, fid, 5)


def cb_update(data, chat_id=ADMIN):
    msg = Message(message_id=1, date=datetime.now(), chat=Chat(id=chat_id, type="private"))
    cb = CallbackQuery(
        id="1", from_user=User(id=chat_id, is_bot=False, first_name="x"), chat_instance="c", message=msg, data=data
    )
    return Update(update_id=2, callback_query=cb)


async def test_feedback_callback_and_auth(tmp_path):
    sf = seed(tmp_path)
    fid = add_find(sf, "Баян", 100)
    t = make(sf)
    await t.dp.feed_update(t.bot, cb_update(f"fb:{fid}:1"))
    with sf() as db:
        assert db.get(Find, fid).feedback == 1
    await t.dp.feed_update(t.bot, cb_update(f"fb:{fid}:-1", chat_id=999))  # чужой чат
    await t.dp.feed_update(t.bot, cb_update("fb:abc:1"))  # мусор не падает
    with sf() as db:
        assert db.get(Find, fid).feedback == 1


def test_feedback_changes_crawl_order():
    a, b = logic_cat(1, "A", prior=100), logic_cat(2, "B", prior=100)
    assert [c.id for c in crawl_order([a, b], 1, NOW, {2: 2})] == [2, 1]  # 👍 поднимает
    assert [c.id for c in crawl_order([a, b], 1, NOW, {1: -2})] == [2, 1]  # 👎 опускает
    c1, c2 = logic_cat(1, "A", crawled_days=2, vpd=100), logic_cat(2, "B", crawled_days=2, vpd=100)
    assert [c.id for c in crawl_order([c1, c2], 1, NOW, {2: 1})] == [2, 1]
    assert feedback_factor(100) == 2.5 and feedback_factor(-100) == 0.25 and feedback_factor(0) == 1


# --- G2/G3 в командах ---


def _aimiko(sf):
    for t, v, _p in zip(
        ("Aimiko u2 pro premium 3000w", "Aimiko U2 Pro 63V/65Ah", "Aimiko u2 PRO 3000w", "Aimiko u2 premium"),
        (535, 411, 134, 63),
        (70000, 49900, 120000, 120000),
        strict=True,
    ):
        add_find(sf, t, v, key=t.lower(), cat_id=1)


def test_top_has_models_block_and_margin(tmp_path):
    sf = seed(tmp_path)
    _aimiko(sf)
    with sf() as db:
        f = db.scalars(select(Find).order_by(Find.vpd.desc())).first()
        f.china_price = 2300
        db.commit()
        text = "\n".join(top_messages(db, 7, NOW))
    assert "🔁 Модели с несколькими объявлениями" in text and "aimiko u2 ×4" in text
    assert "💱 ~" in text and "%)" in text  # маржа в строке: цена Китая известна


async def test_price_handler(tmp_path):
    sf = seed(tmp_path)
    fid = add_find(sf, "Bugaboo Dragonfly коляска", 200, cat_id=1)
    t = make(sf)
    await send(t, f"/price {fid} 2300")
    reply = t.session.requests[-1].text
    assert f"Находка #{fid}" in reply and "<code>12 кг</code>" in reply and "маржа" in reply.lower()
    with sf() as db:
        assert db.get(Find, fid).china_price == 2300
    await send(t, "/price 999 100")
    assert "не найдена" in t.session.requests[-1].text
    await send(t, "/price abc")
    assert t.session.requests[-1].text.startswith("/price <номер")


async def test_summary_models_block_in_run(tmp_path):
    t = life(tmp_path, {"A": [900, 900]})
    for n in (1, 2):  # две находки одной модели в разных подкатегориях
        t.pages[url_of(f"Ac{n}")] = search_html((f"M{n}", f"Pioneer DDJ 400 версия {n}", 20000 + n, "1 день назад"))
        t.pages[card_url(f"M{n}")] = item_html(900)
    t.crawler.start()
    await t.crawler._task
    assert "pioneer ddj 400 ×2" in summary_text(t)
