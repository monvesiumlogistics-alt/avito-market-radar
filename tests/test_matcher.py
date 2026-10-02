from types import SimpleNamespace

from app.config import Settings, split_csv
from app.models import Listing
from app.services.matcher import matches, normalize

s = Settings(_env_file=None)
RULE = SimpleNamespace(
    keywords=split_csv(s.keywords), exclude_keywords=split_csv(s.exclude_keywords), price_min=0, price_max=2000
)


def item(title: str, price: int | None = 1500, description: str | None = None) -> Listing:
    return Listing(external_id="1", title=title, price=price, url="https://www.avito.ru/x/y_1", description=description)


def test_normalize():
    assert normalize("Dolce&Gabbana") == "dolce gabbana"
    assert normalize("Дольче и Габбана, ёлка") == "дольче габбана елка"


def test_brand_spellings_match():
    for title in [
        "Куртка Dolce & Gabbana",
        "DOLCE&GABBANA ремень",
        "Футболка Dolce Gabbana",
        "Сумка Дольче Габбана",
        "Очки дольче и габбана",
        "Лонгслив Dolce Gabanna japan",
        "dolcegabbana кепка",
    ]:
        assert matches(item(title), RULE), title


def test_other_brands_do_not_match():
    assert not matches(item("Кожаные туфли Officine Creative"), RULE)
    assert not matches(item("Gabbana style shirt"), RULE)


def test_keyword_in_description():
    assert matches(item("Рубашка мужская", description="бренд Dolce & Gabbana, оригинал"), RULE)


def test_exclusions():
    assert not matches(item("Dolce Gabbana реплика"), RULE)
    assert not matches(item("Dolce&Gabbana LUX качество"), RULE)
    assert not matches(item("Сумка Dolce Gabbana 1:1"), RULE)
    assert not matches(item("Dolce Gabbana", description="копия под оригинал"), RULE)
    assert matches(item("Dolce Gabbana Luxor"), RULE)  # 'lux' только целым словом


def test_price_bounds():
    assert matches(item("Dolce Gabbana", 2000), RULE)
    assert not matches(item("Dolce Gabbana", 2001), RULE)
    assert not matches(item("Dolce Gabbana", None), RULE)
    assert not matches(item("Dolce Gabbana", 0), RULE)
