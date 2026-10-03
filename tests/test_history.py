"""ADR-015: история рынка — каждое увиденное объявление (ads), изменения (ad_events), замеры карточек (card_obs),
итог выдачи категории за прогон (scan_categories). Поведение /report не меняется."""

from datetime import timedelta

import pytest

from app.db import Ad, AdEvent, CardObs, Find, ScanCategory
from app.models import Listing
from app.providers.avito_parser import parse_total_count
from app.providers.base import ProviderBlocked
from app.services.history import record_search
from scripts.backfill_history import backfill
from tests.test_market import (
    FIXTURES,
    NOW,
    SELLER_URL,
    card_url,
    item_html,
    rows,
    search_html,
    search_url,
    seller_html,
    setup,
)


def counted(html: str, total: int) -> str:
    return html.replace("<body>", f'<body><span data-marker="page-title/count">{total:,}</span>'.replace(",", " "))


def listing(i, price=20000, title="Баян Weltmeister 120"):
    return Listing(external_id=str(i), title=title, price=price, url=f"https://www.avito.ru/x/y/t_{i}?context=abc",
                   location="moskva", published_at=NOW - timedelta(days=1))  # fmt: skip


# --- парсер ---


def test_total_count_from_fixtures():
    assert parse_total_count((FIXTURES / "market_search_s104.html").read_text(encoding="utf-8")) == 17888
    assert parse_total_count((FIXTURES / "market_section.html").read_text(encoding="utf-8")) == 522971
    assert parse_total_count("<html></html>") is None


# --- запись выдачи ---


def test_record_search_upserts_and_logs_changes(tmp_path):
    t = setup(tmp_path, {})
    with t.sf() as db:
        assert record_search(db, t.cat_id, [listing(1), listing(2), listing(3)], {"3"}, NOW) == (2, 0)
        db.commit()
    later = NOW + timedelta(hours=5)
    with t.sf() as db:
        changed = [listing(1, price=18000), listing(2, title="Баян Weltmeister 120 басов"), listing(4)]
        assert record_search(db, t.cat_id, changed, set(), later) == (1, 2)
        db.commit()
    ads = {a.id: a for a in rows(t, Ad)}
    assert set(ads) == {"1", "2", "3", "4"}
    a1 = ads["1"]
    assert (a1.price, a1.first_seen_at, a1.last_seen_at) == (18000, NOW, later)
    assert a1.url_path == "/x/y/t_1" and a1.city == "moskva" and a1.model_key == "weltmeister 120"
    assert a1.posted_src == "search" and a1.status == "live"
    assert ads["3"].promoted_seen == 1 and ads["3"].last_seen_at == NOW
    events = sorted((e.ad_id, e.kind, e.old, e.new) for e in rows(t, AdEvent))
    assert events == [
        ("1", "price", "20000", "18000"),
        ("2", "title", "Баян Weltmeister 120", "Баян Weltmeister 120 басов"),
    ]


# --- /report пишет историю ---


async def test_report_crawl_records_ads_scan_and_card_obs(tmp_path):
    pages = {
        search_url(1): counted(
            search_html(
                ("1", "Баян Weltmeister", 20000, "1 день назад"),
                ("2", "Баян Тула", 30000, "2 дня назад"),
                ("3", "Баян старый", 25000, "10 дней назад"),  # старше недели: в находки нет, в историю — да
            ),
            321,
        ),
        card_url(1): item_html(300),
        card_url(2): item_html(30, seller=False),
        SELLER_URL: seller_html("1", "29 сентября в 12:00"),
    }
    t = setup(tmp_path, pages)
    assert await t.crawler.crawl_subcategory(t.cat_id)
    assert {a.id for a in rows(t, Ad)} == {"1", "2", "3"}
    sc = rows(t, ScanCategory)
    assert len(sc) == 1
    s = sc[0]
    assert (s.run_id, s.category_id, s.pages, s.cards_seen, s.new_ads, s.known_ads) == (t.run_id, t.cat_id, 1, 3, 3, 0)
    assert (s.total_count, s.stop_reason, s.window_hours) == (321, "age_limit", 7 * 24)
    obs = sorted((o.ad_id, o.views, o.today, o.bucket, o.run_id) for o in rows(t, CardObs))
    assert obs == [("1", 300, 5, "report", t.run_id), ("2", 30, 5, "report", t.run_id)]
    ads = {a.id: a for a in rows(t, Ad)}
    assert ads["1"].posted_src == "seller" and ads["1"].posted_at == NOW - timedelta(days=4)
    assert ads["1"].seller_url == SELLER_URL
    assert ads["2"].posted_src == "card" and ads["2"].posted_at == NOW - timedelta(days=3)


async def test_second_crawl_counts_known_and_depth_cap(tmp_path):
    page = search_html(("1", "Баян А", 20000, "1 день назад"), ("2", "Баян Б", 20000, "1 день назад"))
    t = setup(tmp_path, {search_url(1): page, search_url(2): page}, report_max_pages=2, report_cards_per_subcat=0)
    assert await t.crawler.crawl_subcategory(t.cat_id)
    s = rows(t, ScanCategory)[0]
    assert (s.pages, s.cards_seen, s.new_ads, s.known_ads, s.stop_reason) == (2, 4, 2, 2, "depth_cap")
    assert s.total_count is None and s.window_hours == 24


async def test_pages_are_kept_when_block_hits_mid_category(tmp_path):
    pages = {
        search_url(1): search_html(("1", "Баян А", 20000, "1 день назад")),
        search_url(2): ProviderBlocked("captcha"),
    }
    t = setup(tmp_path, pages, report_max_pages=2, captcha_wait_minutes=0)
    with pytest.raises(ProviderBlocked):
        await t.crawler.crawl_subcategory(t.cat_id)
    assert [a.id for a in rows(t, Ad)] == ["1"]  # страница 1 уже в истории, хоть категория и прервана


async def test_empty_first_page_with_listings_is_not_full_coverage(tmp_path):
    t = setup(tmp_path, {search_url(1): counted(search_html(), 5000)})
    assert await t.crawler.crawl_subcategory(t.cat_id)
    s = rows(t, ScanCategory)[0]
    assert (s.stop_reason, s.window_hours, s.total_count) == ("empty", 0, 5000)  # вёрстка сломалась, а не «пусто»
    t2 = setup(tmp_path / "b", {search_url(1): search_html()})  # реально пусто (счётчика нет): как раньше
    assert await t2.crawler.crawl_subcategory(t2.cat_id)
    assert rows(t2, ScanCategory)[0].window_hours == 7 * 24


async def test_recheck_writes_card_obs_and_gone_status(tmp_path):
    t = setup(tmp_path, {}, recheck_max=5)
    old = NOW - timedelta(days=3)
    with t.sf() as db:
        for ext in ("10", "11"):
            db.add(Find(run_id=t.run_id, category_id=t.cat_id, group_key=ext, title="Баян", price_min=1, price_max=1,
                        vpd=100, age_days=1, date_checked=True, copies=1, url=card_url(ext), external_id=ext,
                        hot=True, created_at=old))  # fmt: skip
        db.add(Ad(id="11", category_id=t.cat_id, title="Баян", url_path="/x", first_seen_at=old, last_seen_at=old))
        db.commit()
    t.provider.pages[card_url("10")] = item_html(500)
    t.provider.pages[card_url("11")] = "<title>Объявление снято с публикации</title>"
    await t.crawler._recheck_finds()
    assert [(o.ad_id, o.views, o.bucket) for o in rows(t, CardObs)] == [("10", 500, "recheck")]
    ad = {a.id: a for a in rows(t, Ad)}["11"]
    assert (ad.status, ad.status_at) == ("gone", NOW)
    assert [(e.ad_id, e.kind, e.old, e.new) for e in rows(t, AdEvent)] == [("11", "status", "live", "gone")]


# --- бэкфилл находок ---


def test_backfill_finds_is_idempotent(tmp_path):
    t = setup(tmp_path, {})
    with t.sf() as db:
        db.add(Find(run_id=t.run_id, category_id=t.cat_id, group_key="g", title="Aimiko U2 Pro", price_min=80000,
                    price_max=90000, views=900, today=40, vpd=300, age_days=3, date_checked=True, copies=2,
                    url="https://www.avito.ru/m/v/a_77?ctx=1", external_id="77", hot=True, created_at=NOW,
                    seller_date=NOW - timedelta(days=3), last_checked_at=NOW + timedelta(days=2), views_last=1500,
                    gone_at=NOW + timedelta(days=4)))  # fmt: skip
        db.commit()
    assert backfill(t.sf) == 1
    assert backfill(t.sf) == 0
    ad = rows(t, Ad)[0]
    assert (ad.id, ad.price, ad.url_path, ad.model_key, ad.posted_src, ad.status) == (
        "77", 80000, "/m/v/a_77", "aimiko u2", "seller", "gone"
    )
    obs = sorted((o.views, o.bucket) for o in rows(t, CardObs))
    assert obs == [(900, "backfill"), (1500, "backfill")]
