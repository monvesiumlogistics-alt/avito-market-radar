"""ADR-023 pre-flight: first/last seen товара, переопределение товара при переходе из запроса в категорию,
домен в диагностике, двусмысленные бренды."""

from datetime import timedelta

import pytest
from sqlalchemy import select

from app.db import Ad, Category, Product
from app.models import Listing
from app.services.history import record_search
from app.services.product_store import diagnostics, rebuild
from app.services.products import domain_of, identify
from tests.test_market import NOW, life, rows

D = timedelta(days=1)


def lst(i, title, price=80000, city="moskva"):
    return Listing(external_id=str(i), title=title, price=price, url=f"https://www.avito.ru/{city}/x/t_{i}",
                   published_at=NOW - D)  # fmt: skip


def one_cat(t, url=None):
    with t.sf() as db:
        c = db.scalars(select(Category)).first()
        if url:
            c.url = url
        db.commit()
        return c.id


# --- 0.1 first/last seen товара ---


def test_product_last_seen_follows_resightings_and_rebuild(tmp_path):
    t = life(tmp_path, {"A": [900]})
    cid = one_cat(t)
    for day, ids in ((0, [1]), (1, [2]), (3, [1])):  # третий день: старое объявление снова в выдаче
        with t.sf() as db:
            record_search(db, cid, [lst(i, "Pioneer DDJ-FLX4") for i in ids], set(), NOW + day * D)
            db.commit()
    (p,) = rows(t, Product)
    assert (p.first_seen_at, p.last_seen_at) == (NOW, NOW + 3 * D)
    with t.sf() as db:  # испорченные значения пересборка восстанавливает из объявлений
        db.get(Product, p.id).first_seen_at = NOW + 10 * D
        db.commit()
        rebuild(db, NOW + 5 * D)
    (p,) = rows(t, Product)
    assert (p.first_seen_at, p.last_seen_at) == (NOW, NOW + 3 * D)


# --- 0.2 запрос → настоящая категория ---


def test_query_ad_reidentified_with_real_category_domain(tmp_path):
    t = life(tmp_path, {"A": [900, 900]})
    with t.sf() as db:
        real, query = db.scalars(select(Category)).all()
        query.kind, query.url = "query", "https://www.avito.ru/rossiya?q=lenovo"  # домен generic
        real.url = "https://www.avito.ru/rossiya/noutbuki/lenovo-X"  # домен laptop
        db.commit()
        real_id, query_id = real.id, query.id
    title = "Lenovo Legion 5 Pro 16IRX9 32/1tb"
    assert identify(title, "generic").canonical_key != identify(title, "laptop").canonical_key  # сценарий значим
    with t.sf() as db:
        record_search(db, query_id, [lst(1, title)], set(), NOW, query=True)
        db.commit()
    with t.sf() as db:
        assert db.get(Product, db.get(Ad, "1").product_id).canonical_key == "lenovo|legion-5-pro"
    with t.sf() as db:  # заголовок тот же, но появилась настоящая категория — товар по её домену
        record_search(db, real_id, [lst(1, title)], set(), NOW + D)
        db.commit()
    with t.sf() as db:
        ad = db.get(Ad, "1")
        assert ad.category_id == real_id
        assert db.get(Product, ad.product_id).canonical_key == "lenovo|legion-5-pro|16irx9"


# --- 0.3 диагностика по домену объявления ---


def test_diagnostics_uses_ad_domain_not_generic(tmp_path):
    t = life(tmp_path, {"A": [900]})
    cid = one_cat(t, "https://www.avito.ru/rossiya/noutbuki/honor-X")
    specs = ["i5-12450H 16/512", "i7-13700H 32/1tb", "Ryzen 7 7840HS", "i5-13420H 8/512", "Ultra 5 125H"]
    with t.sf() as db:
        record_search(db, cid, [lst(i, f"Honor MagicBook X16 {s}") for i, s in enumerate(specs, 1)], set(), NOW)
        db.commit()
        d = diagnostics(db, scopes=("CORE",))
    assert d["top"] == [("Honor MagicBook X16", 5)] and d["suspicious"] == []  # процессоры — характеристики


# --- 0.4 двусмысленные бренды ---


@pytest.mark.parametrize(
    ("title", "domain", "not_brand"),
    [
        ("Ремешок для часов Pro Trek Casio PRW-3510Y-8", "watch", "Trek"),  # реальный случай из базы
        ("Gaming PC giant case RTX 4070", "pc", "Giant"),
        ("Ford Focus 3 магнитола Teyes CC3", "auto", "Focus"),
        ("Kitchen essentials набор", "generic", "Fear of God"),
        ("Supreme sound колонка 200W", "hifi", "Supreme"),
        ("Чехол NB 15 дюймов", "laptop", "New Balance"),
        ("Cube 3x3 головоломка", "generic", "Cube"),
    ],
)
def test_ambiguous_brand_only_in_its_domain(title, domain, not_brand):
    assert identify(title, domain).brand != not_brand


def test_ambiguous_brands_still_work_in_their_domain():
    assert identify("Велосипед Trek Madone SL6", "bike").canonical_key == "trek|madone-sl6"
    assert identify("Худи Essentials FOG", "fashion").brand == "Fear of God"
    assert identify("Ford Focus 3 магнитола Teyes CC3", "auto").canonical_key == "teyes|cc3"
    assert domain_of("https://www.avito.ru/rossiya/velosipedy/elektrovelosipedy-X") == "ebike"
    assert domain_of("https://www.avito.ru/rossiya/zapchasti_i_aksessuary/shiny-X") == "auto"
