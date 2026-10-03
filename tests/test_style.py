"""ADR-014: срез рынка по подкатегориям, дедупликация, текст покрытия, единый стиль и премиум-иконки."""

from datetime import timedelta
from types import SimpleNamespace as NS

from app.config import settings
from app.db import Find
from app.services import panels
from app.services.market_cmds import top_finds
from app.services.market_logic import (
    Margin,
    dedupe_finds,
    format_card,
    format_line,
    icon,
    market_line,
    market_lines,
    market_stats,
    stats_lines,
    summary_tail,
)
from tests.test_cmds import add_find, make, seed
from tests.test_handlers import send
from tests.test_market import (
    NOW,
    card_url,
    cat_row,
    item_html,
    life,
    rows,
    search_html,
    search_url,
    setup,
    summary_text,
    url_of,
)
from tests.test_market_logic import find

# --- срез рынка ---


def opened(vpd, i):
    return NS(vpd=vpd, card=NS(url=f"https://www.avito.ru/x_{i}", title=f"T{i}"))


def test_market_stats_pure():
    cards = [NS(price=p) for p in (20000, 30000, 50000, None)]
    st = market_stats(cards, [opened(5, 1), opened(41, 2), opened(15, 3)])
    assert st == {
        "last_fresh_count": 4, "last_price_median": 30000, "last_opened": 3, "last_vpd_min": 5,
        "last_vpd_median": 15, "last_vpd_max": 41, "last_best_url": "https://www.avito.ru/x_2", "last_best_title": "T2",
    }  # fmt: skip
    empty = market_stats([], [])
    assert empty["last_fresh_count"] == 0 and empty["last_vpd_max"] is None and empty["last_best_url"] is None


def cat(name="Аккордеоны", fresh=250, days=1.7, price=45000, opened=12, lo=8, med=15, hi=41, url="https://a.ru/x"):
    return NS(name=name, last_fresh_count=fresh, last_days_covered=days, last_price_median=price, last_opened=opened,
              last_vpd_min=lo, last_vpd_median=med, last_vpd_max=hi, last_best_url=url)  # fmt: skip


def test_market_line_variants_and_order():
    line = market_line(cat())
    assert line == (
        "Аккордеоны — <code>250</code> свежих за <code>1.7</code> дн · ~<code>45 000</code> ₽"
        ' · 👁 <code>8–41</code>/д (медиана <code>15</code>) · <a href="https://a.ru/x">лучшее</a>'
    )
    assert market_line(cat(fresh=0)) == "Аккордеоны — пусто"
    assert "<code>7</code>/д (медиана" in market_line(cat(lo=7, hi=7)) and "👁 <code>7</code>" in market_line(
        cat(lo=7, hi=7)
    )
    no_open = market_line(cat(opened=0, lo=None, med=None, hi=None, url=None))
    assert "страницы не открывали" in no_open and "лучшее" not in no_open
    ordered = market_lines(
        [cat("A", hi=10), cat("B", hi=300), cat("C", opened=0, lo=None, med=None, hi=None), cat("D", hi=50)]
    )
    assert [ln.split(" — ")[0] for ln in ordered] == ["B", "D", "A", "C"]


async def test_crawl_persists_market_stats_even_below_threshold(tmp_path):
    pages = {
        search_url(1): search_html(
            ("1", "Баян А", 20000, "3 дня назад"),
            ("2", "Гитара Б", 30000, "3 дня назад"),
            ("3", "Скрипка В", 40000, "3 дня назад"),
        ),
        card_url(1): item_html(3),
        card_url(2): item_html(6),
        card_url(3): item_html(9),
    }
    t = setup(tmp_path, pages)
    assert await t.crawler.crawl_subcategory(t.cat_id)
    assert rows(t) == []  # vpd 1-3 < VPD_MIN: находок нет, но срез рынка записан
    c = cat_row(t)
    assert (c.last_fresh_count, c.last_price_median, c.last_opened) == (3, 30000, 3)
    assert (c.last_vpd_min, c.last_vpd_median, c.last_vpd_max) == (1, 2, 3)
    assert c.last_best_title == "Скрипка В" and c.last_best_url == card_url(3)
    assert c.last_days_covered == 7.0


async def test_summary_lists_every_crawled_subcategory_with_empty_ones(tmp_path):
    t = life(tmp_path, {"A": [900, 150, 900]})
    t.pages[url_of("Ac2")] = search_html()  # пустая выдача: «пусто»
    t.crawler.start()
    await t.crawler._task
    text = summary_text(t)
    assert "<b>📈 Рынок по подкатегориям — 3</b>" in text
    lines = {ln.split(" — ")[0].replace("<blockquote expandable>", ""): ln for ln in text.split("\n") if " — " in ln}
    assert lines["A 2"].endswith("A 2 — пусто") or "пусто" in lines["A 2"]
    assert (
        "<code>1</code> свежих" in lines["A 1"] and "👁 <code>300</code>/д" in lines["A 1"] and "лучшее" in lines["A 1"]
    )
    assert text.index("A 1 — ") < text.index("A 2 — ")  # по max vpd; пустая подкатегория в конце блока
    assert "пусто" in text.split("A 2 — ")[1].split("\n")[0]


# --- дедупликация ---


def test_dedupe_finds_keeps_best_vpd():
    a = NS(external_id="1", vpd=50, t="low")
    b = NS(external_id="1", vpd=90, t="high")
    c = NS(external_id="2", vpd=10, t="other")
    assert [f.t for f in dedupe_finds([a, c, b])] == ["high", "other"]


async def test_same_listing_not_written_twice_in_run(tmp_path):
    t = setup(tmp_path, {})

    def make_find(**kw):
        return Find(
            category_id=t.cat_id, group_key="g", title="Баян", price_min=1, price_max=1, vpd=100, age_days=1,
            date_checked=True, copies=1, url="u", external_id="77", hot=True, created_at=NOW, **kw,
        )  # fmt: skip

    assert len(t.crawler._save(t.cat_id, finds=[make_find(), make_find()])) == 1  # и внутри одного вызова
    assert t.crawler._save(t.cat_id, finds=[make_find()]) == []  # и повторно в том же прогоне
    assert len(rows(t)) == 1


def test_top_dedupes_same_external_id(tmp_path):
    sf = seed(tmp_path)
    add_find(sf, "Баян Roland", 80, key="a")
    add_find(sf, "Баян Roland", 150, key="b")  # то же объявление под другим group_key
    with sf() as db:
        res = top_finds(db, 7, NOW)
    assert [f.vpd for f in res] == [150]


# --- покрытие недели ---


def test_coverage_line_is_explicit():
    tail = summary_tail([("Balenciaga жен.", 1.7), ("Nike", 3.0)], [], 0, 7, 5)
    assert tail == ["⚠️ Не вся неделя (лимит 5 стр.): Balenciaga жен. — 1.7 дн из 7, Nike — 3 дн из 7"]
    assert "Покрыто" not in tail[0]


# --- стиль и премиум-иконки ---


def test_icon_normalizes_variation_selector_and_falls_back():
    assert 'emoji-id="5870609858520158157"' in icon("ℹ️", True) and 'emoji-id="5870609858520158157"' in icon("ℹ", True)
    assert icon("💵", True).startswith('<tg-emoji emoji-id="5870478797593120516">')
    assert icon("💵") == "💵" and icon("🧩", True) == "🧩"  # без premium и без иконки в наборе — обычный эмодзи


def test_card_line_and_stats_premium_vs_plain():
    f = find(id=5)
    plain, prem = format_card(f), format_card(f, premium=True)
    assert "tg-emoji" not in plain and "💵 <code>" in plain and "🗓" in plain
    for emoji, cid in (("💵", "5870478797593120516"), ("👁", "5870542612217204751"), ("🗓", "5870847962917113498"),
                       ("💡", "5870813306826002498"), ("🔎", "5870974879200711167")):  # fmt: skip
        assert f'<tg-emoji emoji-id="{cid}">{emoji}</tg-emoji>' in prem
    assert "tg-emoji" in format_line(find(gone_at=NOW, created_at=NOW - timedelta(days=2)), premium=True)
    assert "tg-emoji" not in stats_lines(3, 1, 2, 1) and "tg-emoji" in stats_lines(3, 1, 2, 1, premium=True)
    assert stats_lines(3, 0).count("\n") == 0 and "🔥" not in stats_lines(3, 0)


async def test_summary_premium_on_and_off(tmp_path):
    t = life(tmp_path / "off", {"A": [900]})
    t.crawler.start()
    await t.crawler._task
    assert "tg-emoji" not in summary_text(t) and "<b>📊 Проверка рынка · 03.10</b>" in summary_text(t)
    t = life(tmp_path / "on", {"A": [900]}, premium_emoji=True)
    t.crawler.start()
    await t.crawler._task
    text = summary_text(t)
    assert '<b><tg-emoji emoji-id="5870930636742595124">📊</tg-emoji> Проверка рынка · 03.10</b>' in text
    assert '<tg-emoji emoji-id="5870570722778156940">📂</tg-emoji> Подкатегорий: <code>1</code>' in text
    card = next(x for x in t.notifier.sent if "💡" in x)  # 🔥 карточка тоже с иконками
    assert "tg-emoji" in card


def test_panels_plain_and_premium():
    for fn, args in (
        (panels.start_text, ()), (panels.status_header, (False,)), (panels.captcha_passed, ()),
        (panels.block_restored, ()), (panels.captcha_alert, (15,)), (panels.block_alert, ("<x>",)),
        (panels.startup_text, ("Правило", 60, True)),
    ):  # fmt: skip
        assert "tg-emoji" not in fn(*args) and "tg-emoji" in fn(*args, premium=True)
    alert = panels.block_alert("<b>x</b>")
    assert "&lt;b&gt;x&lt;/b&gt;" in alert and "<b>x</b>" not in alert
    assert "<code>15</code> мин" in panels.captcha_alert(15) and panels.captcha_alert(15).startswith("🧩 ")
    assert "каждые <code>60</code> мин" in panels.startup_text("Правило", 60, True)
    assert "Мониторинг" not in panels.startup_text("Правило", 60, False)
    m = Margin(yuan=2300, kg=12, cost=34060, margin=14940, pct=30, air_margin=-1000, air_pct=-2)
    price = panels.price_panel(7, "Bugaboo", 49000, m, 12.2, 500)
    assert "<b>💵 Находка #7</b>" in price and "<code>14 940 ₽ (30%)</code>" in price and "<code>12 кг</code>" in price
    rule = "\n".join(panels.status_rule("R", "ACTIVE", "01.10 12:00", 5, 2, no_urls=True))
    assert "Статус: <code>ACTIVE</code>" in rule and "Нет AVITO_SEARCH_URLS" in rule


async def test_start_status_and_cats_handlers_use_panel_style(tmp_path, monkeypatch):
    sf = seed(tmp_path)
    t = make(sf)
    await send(t, "/start")
    text = t.session.requests[-1].text
    assert "<b>⚙ AvitoHunter</b>" in text and "/report" in text and "tg-emoji" not in text
    monkeypatch.setattr(settings, "premium_emoji", True)
    await send(t, "/start")
    assert "tg-emoji" in t.session.requests[-1].text
    await send(t, "/cats")
    cats = t.session.requests[-1].text
    assert "<b><tg-emoji" in cats and "<code>2 / 0</code>" in cats
