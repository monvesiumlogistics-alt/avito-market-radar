"""ADR-017: Radar v1 — что изменилось на рынке по выдаче: темп новых объявлений категории против её нормы,
модели у многих продавцов, новые модели; покрытие и уверенность у каждого вывода."""

from datetime import timedelta
from types import SimpleNamespace as NS

from sqlalchemy import select

from app.db import Ad, Category, CrawlRun, ScanCategory
from app.services.market_logic import tg_len
from app.services.radar import (
    category_trend,
    confidence,
    emerging_models,
    frequent_models,
    label,
    scan_velocity,
)
from tests.test_handlers import make, send
from tests.test_market import NOW, life, rows

H = timedelta(hours=1)
D = timedelta(days=1)


def sc(at, new=0, seen=0, window=0.0, stop="known", total=None):
    return NS(at=at, new_ads=new, cards_seen=seen, window_hours=window, stop_reason=stop, total_count=total)


# --- темп категории ---


def test_scan_velocity_incremental_vs_first_and_partial():
    prev = sc(NOW - D)
    assert scan_velocity(sc(NOW, new=30), prev) == (30, 1.0)  # 30 новых за сутки с прошлого обхода
    assert scan_velocity(sc(NOW, new=30, stop="age_limit"), sc(NOW - 2 * D)) == (15, 1.0)
    assert scan_velocity(sc(NOW, seen=70, window=168, stop="age_limit"), None) == (10, 1.0)  # первый: плотность
    v, cov = scan_velocity(sc(NOW, new=150, seen=150, window=12, stop="depth_cap"), prev)
    assert (v, cov) == (300, 0.5)  # лимит страниц: темп по плотности, покрыто 12 ч из 24
    assert scan_velocity(sc(NOW, new=5), sc(NOW - 0.5 * H)) == (None, 0.0)  # слишком близко — не оценка


def series(*per_day, stop="known", start=NOW - 6 * D):
    """Обходы раз в сутки с заданным числом новых; первый — по плотности (неделя, per_day[0] в сутки)."""
    out = [sc(start, seen=per_day[0] * 7, window=168, stop="age_limit")]
    for i, n in enumerate(per_day[1:], 1):
        out.append(sc(start + i * D, new=n, stop=stop, total=1000 + 10 * i))
    return out


def test_category_trend_growing_cooling_flat_and_too_short():
    up = category_trend(series(50, 55, 48, 52, 50, 51, 130), NOW)
    assert (up.state, round(up.ratio, 1), up.today, up.base) == ("growing", 2.6, 130, 50.5)
    assert up.history_days == 6 and up.total == 1060 and up.total_change == 1060 / 1010 - 1  # к самому раннему за 7 дн
    down = category_trend(series(50, 55, 48, 52, 50, 51, 15), NOW)
    assert down.state == "cooling"
    assert category_trend(series(50, 55, 48, 52, 50, 51, 60), NOW).state == "stable"  # в пределах шума
    assert category_trend(series(3, 2, 4, 3, 2, 3, 9), NOW).state == "stable"  # мало: ×3, но 9 в сутки — шум
    short = category_trend(series(50, 50, 130, start=NOW - 2 * D), NOW)
    assert short.state == "unknown"  # < 3 дней до сегодня
    assert category_trend(series(50, 55, 48, 52, 50, 51, start=NOW - 7 * D), NOW).state == "unknown"  # сегодня не были


def test_confidence_weakest_link():
    assert confidence(n=100, need=20, history_days=14, coverage=1) == 1
    assert confidence(n=100, need=20, history_days=3, coverage=1) == 3 / 14  # короткая история
    assert confidence(n=5, need=20, history_days=30, coverage=1) == 0.25
    assert confidence(n=100, need=20, history_days=30, coverage=0.5, persistent=False) == 0.4
    assert [label(x) for x in (0.9, 0.5, 0.2)] == ["HIGH", "MEDIUM", "LOW"]


# --- модели ---


def ad(key, city, price=50000, days_ago=1, shop=None, cat=1):
    return NS(model_key=key, url_path=f"/{city}/velosipedy/x_1", price=price, shop=shop, category_id=cat,
              posted=NOW - timedelta(days=days_ago))  # fmt: skip


def test_frequent_models_need_several_cities():
    ads = [ad("aimiko u2", c, p) for c, p in (("moskva", 80000), ("spb", 90000), ("kazan", 85000))]
    ads += [ad("aimiko u2", "moskva", 88000, shop="Магазин"), ad("shop x1", "moskva"), ad("shop x1", "moskva")]
    ads += [ad("old m1", "moskva", days_ago=9), ad("old m1", "spb", days_ago=9), ad("old m1", "ufa", days_ago=9)]
    (m,) = frequent_models(ads, NOW)  # «shop x1» — 1 город, «old m1» — старше 7 дней
    assert (m.key, m.count, m.cities, m.shops, m.price, m.category_id) == ("aimiko u2", 4, 3, 1, 86500, 1)


def test_emerging_models_new_cluster_only():
    spots = (("moskva", 0), ("spb", 1), ("kazan", 1), ("ufa", 2), ("omsk", 2))
    fresh = [ad("doona x", c, days_ago=d) for c, d in spots]
    steady = [ad("bugaboo fox", c, days_ago=d) for c, d in (("moskva", 1), ("spb", 1), ("kazan", 2), ("ufa", 1))]
    steady += [ad("bugaboo fox", c, days_ago=d) for c, d in (("moskva", 6), ("spb", 8), ("kazan", 10))]
    few_cities = [ad("one shop", "moskva", days_ago=1) for _ in range(6)]
    (m,) = emerging_models(fresh + steady + few_cities, NOW)
    assert (m.key, m.count, m.prior, m.cities) == ("doona x", 5, 0, 5)


# --- отчёт ---


def seed(t, days=10):
    """Одна категория: обходы раз в сутки по 50 новых; сегодня час назад 139 (+1 найдёт сам обход) = 140 за сутки;
    модель в 5 городах."""
    with t.sf() as db:
        cat = db.scalars(select(Category)).first()
        for i in [*range(days, 0, -1), 0]:
            at = NOW - i * D if i else NOW - H
            new = 139 if i == 0 else 50
            run = CrawlRun(started_at=at, finished_at=at, kind="sweep", status="done")
            db.add(run)
            db.flush()
            db.add(ScanCategory(run_id=run.id, category_id=cat.id, at=at, pages=2, cards_seen=new, new_ads=new,
                                known_ads=40, total_count=3000 + 20 * (days - i), window_hours=30,
                                stop_reason="age_limit" if i == days else "known", done=True))  # fmt: skip
        for n, city in enumerate(("moskva", "spb", "kazan", "ufa", "omsk")):
            db.add(Ad(id=f"d{n}", category_id=cat.id, title="Doona X", model_key="doona x", price=45000 + n * 1000,
                      url_path=f"/{city}/tovary/doona_{n}", posted_at=NOW - D, first_seen_at=NOW - D,
                      last_seen_at=NOW))  # fmt: skip
        db.commit()


async def test_sweep_end_sends_one_radar_message(tmp_path):
    t = life(tmp_path, {"A": [900]})
    seed(t)
    t.crawler.start("sweep")
    await t.crawler._task
    text = t.notifier.sent[-1]
    assert text.startswith("<b>📡 MARKET RADAR · 03.10</b>")
    assert "Покрытие: <code>1 / 1</code> категорий" in text
    assert "🔥 Предложение растёт" in text and "A 1" in text and "×" in text
    assert "🔁 Модели у многих продавцов" in text and "Doona X" in text and "5 городов" in text
    assert "<blockquote expandable><b>📡 Обход рынка" in text  # телеметрия обхода — в том же сообщении
    assert tg_len(text) <= 4096


async def test_radar_without_history_says_so(tmp_path):
    t = life(tmp_path, {"A": [900]})
    t.crawler.start("sweep")
    await t.crawler._task
    text = t.notifier.sent[-1]
    assert "Тренды категорий появятся после 3 дней истории" in text
    assert "🔥 Предложение растёт" not in text


async def test_radar_command():
    t = make()
    await send(t, "/radar")
    assert t.crawler.calls == ["radar"]


def test_radar_texts_on_demand(tmp_path):
    t = life(tmp_path, {"A": [900]})
    seed(t)
    (text,) = t.crawler.radar_texts()
    assert "MARKET RADAR" in text and "Обход рынка" not in text
    assert len(rows(t, ScanCategory)) == 11  # только чтение
