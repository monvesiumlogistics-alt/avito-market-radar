"""H1: перепроверка прошлых находок, «ушло за N дней» (ADR-010)."""

from datetime import timedelta
from types import SimpleNamespace as NS

from app.db import Find
from app.providers.avito_parser import is_gone
from app.services.market_logic import format_find, format_gone, format_models, model_groups
from tests.test_cmds import add_find  # noqa: F401
from tests.test_market import FIXTURES, NOW, card_url, item_html, life, rows, runs, summary_text

GONE = "<html><body><h1>Объявление снято с публикации</h1></body></html>"


def old_find(t, ext, days_ago, title="Bugaboo Dragonfly", price=49000, vpd=200):
    with t.sf() as db:
        db.add(
            Find(
                run_id=1, category_id=1, group_key=title.lower(), title=title, price_min=price, price_max=price,
                vpd=vpd, age_days=2.0, date_checked=True, copies=1, url=card_url(ext), external_id=ext, hot=True,
                created_at=NOW - timedelta(days=days_ago), views=100, sent=True,
            )
        )  # fmt: skip
        db.commit()


def run_life(tmp_path, **kw):
    t = life(tmp_path, {"A": [900]}, **kw)
    return t


async def go(t):
    t.crawler.start()
    await t.crawler._task


def by_ext(t):
    return {f.external_id: f for f in rows(t)}


def test_is_gone_markers_and_status():
    assert is_gone("<html></html>", status=404) and is_gone("<html></html>", status=410)
    assert is_gone(GONE) and is_gone("<html></html>", "Объявление закрыто — Avito")
    assert not is_gone(item_html(10) + "Объявление снято с публикации")  # живой счётчик просмотров
    assert not is_gone("<html><body>Купить баян</body></html>")
    for name in ("market_item.html", "market_item_reserved.html"):  # «Товар зарезервирован» — не снято
        assert not is_gone((FIXTURES / name).read_text(encoding="utf-8"))
    assert not is_gone("<body>" + "x " * 3000 + "Товар продан</body>")  # маркер глубоко в тексте


async def test_gone_alive_young_old_and_summary(tmp_path):
    t = run_life(tmp_path)
    old_find(t, "g1", 3, "Bugaboo Dragonfly")
    old_find(t, "a1", 5, "Cybex Eezy S")
    old_find(t, "y1", 1, "Young")  # моложе 2 дней
    old_find(t, "o1", 20, "Old")  # старше 14 дней
    t.pages[card_url("g1")] = GONE
    t.pages[card_url("a1")] = item_html(777)
    await go(t)
    f = by_ext(t)
    assert f["g1"].gone_at == NOW and f["g1"].last_checked_at == NOW
    assert f["a1"].gone_at is None and f["a1"].views_last == 777 and f["a1"].last_checked_at == NOW
    assert f["y1"].last_checked_at is None and f["o1"].last_checked_at is None
    assert card_url("y1") not in t.provider.calls and card_url("o1") not in t.provider.calls
    text = summary_text(t)
    assert "✅ Ушло: 1 (за ~3 дн)" in text and "Bugaboo Dragonfly" in text and "ушло за 3 дн" in text
    assert "Cybex" not in text
    assert runs(t)[0].loads == t.crawler.loads  # перепроверка в бюджете и в статистике


async def test_recheck_limit_disabled_duplicates_and_budget(tmp_path):
    t = run_life(tmp_path / "a", recheck_max=1)
    for i in (1, 2, 3):
        old_find(t, f"x{i}", 3)
        t.pages[card_url(f"x{i}")] = item_html(10)
    await go(t)
    assert sum(card_url(f"x{i}") in t.provider.calls for i in (1, 2, 3)) == 1

    t = run_life(tmp_path / "b", recheck_max=0)
    old_find(t, "x1", 3)
    await go(t)
    assert card_url("x1") not in t.provider.calls

    t = run_life(tmp_path / "c")
    old_find(t, "d1", 3)
    old_find(t, "d1", 4)  # то же объявление найдено в двух прогонах
    t.pages[card_url("d1")] = GONE
    await go(t)
    assert t.provider.calls.count(card_url("d1")) == 1 and all(f.gone_at for f in rows(t) if f.external_id == "d1")
    assert summary_text(t).count("✅ Ушло: 1") == 1

    t = run_life(tmp_path / "d", report_budget=2)  # warm-up + одна перепроверка, на обход бюджета нет
    for i in (1, 2):
        old_find(t, f"b{i}", 3)
        t.pages[card_url(f"b{i}")] = item_html(10)
    await go(t)
    assert sum(card_url(f"b{i}") in t.provider.calls for i in (1, 2)) == 1


async def test_recheck_error_skipped_and_block_follows_adr005(tmp_path):
    from app.providers.base import ProviderBlocked

    t = run_life(tmp_path / "e")
    old_find(t, "e1", 3)
    t.pages[card_url("e1")] = RuntimeError("boom")
    await go(t)
    assert by_ext(t)["e1"].last_checked_at is None and runs(t)[0].status == "done"

    t = run_life(tmp_path / "f", headless=True)
    old_find(t, "k1", 3)

    def blocked():
        raise ProviderBlocked("Доступ ограничен")

    t.pages[card_url("k1")] = blocked
    await go(t)
    assert runs(t)[0].status == "blocked"


def test_format_gone_find_line_and_models():
    gone_at = NOW
    f = NS(title="Bugaboo", url="https://www.avito.ru/x_1", price_min=1000, price_max=1000, vpd=200,
           created_at=NOW - timedelta(days=4), gone_at=gone_at, hot=True, today=None, age_days=2, date_checked=True,
           copies=1, group_key="g", external_id="1")  # fmt: skip
    assert "✅ ушло за 4 дн" in format_find(f, "c")
    assert format_gone([]) == []
    assert "ушло за 4 дн · 200/день · 1 000 ₽" in format_gone([f])[1]
    g = NS(**{**f.__dict__, "external_id": "2", "gone_at": None, "title": "Bugaboo Dragonfly blue"})
    f.title = "Bugaboo Dragonfly red"
    assert "✅ ушло 1" in format_models(model_groups([f, g]))[1]
