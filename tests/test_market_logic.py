from datetime import datetime, timedelta
from types import SimpleNamespace as NS

from app.config import Settings
from app.models import Listing
from app.services.market_logic import (
    SECTION_EMOJI,
    Entry,
    age_days,
    crawl_order,
    date_checked,
    find_age,
    format_card,
    format_progress,
    format_summary,
    group_cards,
    group_entries,
    is_find,
    is_hot,
    norm_title,
    pick_cards,
    reason,
    render_groups,
    section_emoji,
    split_message,
)

NOW = datetime(2026, 10, 3, 12, 0)
S = Settings(_env_file=None)


def card(i: str, title: str, price: int, days: float | None = 3) -> Listing:
    published = NOW - timedelta(days=days) if days is not None else None
    return Listing(external_id=i, title=title, price=price, url=f"https://www.avito.ru/x_{i}", published_at=published)


def test_age_days_min_one():
    assert age_days(NOW - timedelta(hours=3), NOW) == 1.0
    assert age_days(NOW - timedelta(days=3), NOW) == 3.0


def test_find_age_page_date_wins_search_fallback():
    page, search = NOW - timedelta(days=4), NOW - timedelta(days=1)
    assert find_age(page, search, NOW) == 4.0  # m3
    assert find_age(None, search, NOW) == 1.0
    assert find_age(None, None, NOW) is None


def test_date_checked_when_seller_check_disabled():
    assert date_checked(None, check_seller=False)  # m4
    assert not date_checked(None, check_seller=True)
    assert date_checked(NOW, check_seller=True)


def test_norm_title():
    assert norm_title("Pioneer XDJ-RX3 новый") == norm_title("pioneer xdj rx3 ОРИГИНАЛ") == "pioneer xdj rx3"
    assert norm_title("Pioneer XDJ-RX2") != norm_title("Pioneer XDJ-RX3")


def test_group_cards_price_spread():
    cards = [
        card("1", "iPhone 15", 50000),
        card("2", "iphone 15 новый", 58000),
        card("3", "iPhone 15", 61000),
        card("4", "iPhone 14", 40000),
        card("5", "Без цены", 0),
    ]
    ids = sorted(sorted(c.external_id for c in g) for g in group_cards(cards))
    assert ids == [["1", "2"], ["3"], ["4"]]  # 61000 > 50000 * 1.2, без цены отброшена


def test_pick_cards_oldest_groups_and_cards_first():
    g_young = [card("1", "a", 10000, 2), card("2", "a", 10000, 3), card("3", "a", 10000, 1)]
    g_old = [card("4", "b", 10000, 6)]
    g_undated = [card("5", "c", 10000, None)]
    picked = pick_cards([g_young, g_undated, g_old])
    assert [[c.external_id for c in g] for g in picked] == [["4"], ["2", "1"], ["5"]]


def test_pick_cards_no_slot_reservation_m2():
    groups = [[card(str(i), f"t{i}", 10000, 2), card(f"{i}b", f"t{i}", 10000, 1)] for i in range(20)]
    picked = pick_cards(groups)
    assert len(picked) == 20 and all(len(g) == 2 for g in picked)  # кап 12 считает краулер


def test_is_find_is_hot():
    assert is_find(50, 10000, 7, S) and is_hot(100, S) and not is_hot(99, S)
    assert not is_find(49, 10000, 3, S)
    assert not is_find(80, 9999, 3, S)
    assert not is_find(80, 10000, 7.5, S)


def cat(
    i: int, section: str, crawled_days: float | None = None, vpd: int | None = None, run: int | None = None, prior=None
):
    crawled = NOW - timedelta(days=crawled_days) if crawled_days is not None else None
    return NS(id=i, section=section, last_run_id=run, last_best_vpd=vpd, last_crawled_at=crawled, prior_score=prior)


def test_crawl_order():
    cats = [
        cat(1, "A", 10),  # приоритет 10
        cat(2, "A", 2, vpd=500),  # 2 * 6 = 12
        cat(3, "B", 5),  # 5
        cat(4, "B", None),  # ни разу не обходили -> первым
        cat(5, "C", 30, run=7),  # пройдена в этом прогоне -> исключена
        cat(6, "C", 40, run=6),  # пройдена в другом прогоне -> 40
    ]
    # разделы: B (inf), C (40), A (12); внутри раздела по приоритету
    assert [c.id for c in crawl_order(cats, run_id=7, now=NOW)] == [4, 3, 6, 2, 1]


def find(**kw):
    base = dict(
        title="Pioneer XDJ-RX3", url="https://www.avito.ru/x_1", price_min=95000, price_max=110000, vpd=180,
        today=40, age_days=3.2, date_checked=True, copies=2, hot=True, group_key="g",
    )  # fmt: skip
    return NS(**(base | kw))


def test_format_card_fields():
    lines = format_card(find(views=540, page_date=datetime(2026, 10, 1))).split("\n")
    assert lines[0] == '🔥 <a href="https://www.avito.ru/x_1">Pioneer XDJ-RX3</a> ×2'
    assert lines[1] == "💰 95 000–110 000 ₽ · 👁 180/день (+40 сегодня, всего 540) · 📅 01.10 (3 дн)"
    assert lines[2] == (
        "💡 очень высокий спрос: 180 просм/день; выставлено 2 раза — товар ходовой;"
        " есть бренд/модель — легко найти на goofish"
    )
    assert lines[3] == '<a href="https://www.goofish.com/search?q=Pioneer+XDJ-RX3">🔎 goofish</a>' and len(lines) == 4
    card = format_card(
        find(
            price_max=95000, today=None, date_checked=False, copies=1, hot=False, id=7, page_date=datetime(2026, 9, 29)
        ),
        datetime(2026, 10, 1),
        margin="маржа ~1 ₽",
    ).split("\n")
    assert card[0].startswith("<a ") and "×" not in card[0]
    assert card[1] == "💰 95 000 ₽ · 👁 180/день · 📅 29.09 (дата не проверена) · уже было 01.10"
    assert card[3] == "💱 маржа ~1 ₽" and card[4].endswith("🔎 goofish</a> · #7")
    assert format_card(find(date_checked=False)).split("\n")[1].endswith("📅 дата не проверена")  # даты нет совсем
    assert " ~1 дн" in format_card(find(age_days=0.4))


def test_reason_rules_priority_and_cap():
    f = find(copies=1, hot=False, vpd=60, age_days=3, title="Коляска", price_min=20000)
    assert reason(f) == "спрос 60 просм/день"
    assert (
        reason(find(copies=1, vpd=60, age_days=0.5, title="Коляска", price_min=1))
        == "спрос 60 просм/день; свежее (≤1 дн)"
    )
    assert "модель встречается 3 раза" in reason(f, model_count=3)
    assert reason(find(copies=5, vpd=150, age_days=3, title="Коляска")).startswith(
        "очень высокий спрос: 150 просм/день; выставлено 5 раз — товар ходовой"
    )
    rich = find(copies=2, vpd=150, age_days=0.5, price_min=100000, gone_at=NOW, created_at=NOW - timedelta(days=3))
    r = reason(rich, model_count=2).split("; ")
    assert len(r) == 3 and r[0] == "✅ ушло за 3 дн — реально покупают" and r[1].startswith("очень высокий")
    only = reason(find(copies=1, vpd=60, age_days=3, price_min=100000, title="Колонка JBL Flip"), min_price=10000)
    assert only == "спрос 60 просм/день; есть бренд/модель — легко найти на goofish; дорогой чек — маржа в рублях выше"


def test_format_card_hostile_title_escaped_and_capped():
    line = format_card(find(title='<script>"x"</script>' + "A" * 500, url='https://a.ru/?q="><b>'))
    assert "<script>" not in line and '"><b>' not in line and "…</a>" in line
    assert len(line.split("\n")[0]) < 200


def entry(title, vpd, section, sub, **kw):
    f = find(title=title, vpd=vpd, copies=1, hot=vpd >= 100, **kw)
    return Entry(f, section, sub, format_card(f))


AIMIKO = (
    "Aimiko u2 pro premium 3000w",
    "Aimiko U2 Pro 63V/65Ah 9a Strong",
    "Aimiko u2 PRO 3000w",
    "Aimiko u2 premium 3000w",
)


def test_group_entries_aimiko_and_stroller():
    bike = ("velosipedy", "Электровелосипеды")
    kids = ("tovary_dlya_detey_i_igrushki", "Коляски")
    entries = [entry(t, v, *bike) for t, v in zip(AIMIKO, (63, 535, 134, 411), strict=True)]
    entries.insert(2, entry("Bugaboo Dragonfly", 600, *kids))
    groups = group_entries(entries)
    assert [len(g) for g in groups] == [1, 4]  # группа по лучшему vpd: коляска 600 раньше Aimiko 535
    assert [e.find.vpd for e in groups[1]] == [535, 411, 134, 63]
    blocks = render_groups(groups)
    assert len(blocks) == 5
    assert blocks[0].startswith("<b>🧸 ТОВАРЫ ДЛЯ ДЕТЕЙ И ИГРУШКИ — Коляски</b>\n")
    assert blocks[1].startswith("<b>🚲 ВЕЛОСИПЕДЫ — Электровелосипеды</b>\n🔥 <a ") and "Aimiko U2 Pro 63V" in blocks[1]
    assert all("<b>" not in b for b in blocks[2:])  # заголовок только у первой карточки группы
    numbered = render_groups(groups, numbered=True)
    assert numbered[0].split("\n")[1].startswith("1. ") and numbered[4].startswith("5. ")
    assert section_emoji("unknown_slug") == "📦" and section_emoji("telefony") == "📱"


def test_every_top_section_has_an_emoji():
    from app.config import TOP_SECTIONS, split_csv

    assert set(split_csv(TOP_SECTIONS)) <= set(SECTION_EMOJI)


def test_split_message_never_cuts_a_card():
    cards = [f"card {i}\n" + "x" * 300 + "\nline3" for i in range(12)]
    chunks = split_message("\n\n".join(cards), 1000)
    assert len(chunks) > 1 and all(len(c) <= 1000 for c in chunks)
    assert [b for c in chunks for b in c.split("\n\n")] == cards  # только по границам карточек
    huge = "head\n\n" + "\n".join("y" * 90 for _ in range(30))  # блок больше лимита режется по строкам
    out = split_message(huge, 500)
    assert out[0] == "head" and all(len(c) <= 500 for c in out)


def test_format_progress_bar_eta_and_states():
    from app.services.market_logic import progress_bar

    assert (
        progress_bar(228, 600) == "▓▓▓▓░░░░░░"
        and progress_bar(0, 600) == "░" * 10
        and progress_bar(700, 600) == "▓" * 10
    )
    assert progress_bar(1, 0) == "░" * 10
    text = format_progress(754, 228, 600, 31, 9, 4, "🚲 Велосипеды — Электровелосипеды")
    assert text.split("\n") == [
        "⏳ Проверка рынка — 12:34",
        "▓▓▓▓░░░░░░ 38% · загрузок 228/600 · осталось ~21 мин",  # 754 / 228 * 372 = 1230 c = 20.5 мин
        "Сейчас: 🚲 Велосипеды — Электровелосипеды",
        "Подкатегорий 31 · находок 9 (🔥 4)",
    ]
    assert "осталось" not in format_progress(60, 9, 600, 1, 0)  # до 10 загрузок оценки нет
    assert "осталось" not in format_progress(60, 600, 600, 1, 0) and "Сейчас" not in format_progress(60, 5, 600, 1, 0)
    assert format_progress(3725, 10, 600, 1, 0).startswith("⏳ Проверка рынка — 62:05")
    done = format_progress(754, 300, 600, 40, 5, 2, "x", "done").split("\n")
    assert done[0] == "✅ Проверка завершена — 12:34" and "осталось" not in done[1] and len(done) == 3
    assert format_progress(5, 1, 600, 0, 0, status="stopped").startswith("⏹ Остановлено\n")
    assert format_progress(5, 1, 600, 0, 0, status="blocked").startswith("⚠️ Блок Avito\n")
    assert format_progress(5, 1, 600, 0, 0, status="failed").startswith("⚠️ Проверка упала")


def test_summary_covered_days_remaining_and_error_cap():
    errors = [f"<boom {i}>" for i in range(13)]
    text = format_summary(NOW, ["line1", "line2"], [("Телефоны", 2.0), ("Ноутбуки", 3.46)], errors, remaining=140)
    lines = text.split("\n")
    assert lines[0] == "<b>Итог проверки — 03.10</b>" and text.split("\n\n")[1:3] == ["line1", "line2"]
    assert "Покрыто не полностью: Телефоны — 2 из 7 дн, Ноутбуки — 3.5 из 7 дн" in lines
    assert sum(1 for x in lines if x.startswith("ошибка:")) == 10 and "и ещё 3" in lines
    assert "&lt;boom 0&gt;" in text and "<boom" not in text
    assert lines[-1] == "Осталось 140 подкатегорий, пойдут первыми в следующий раз."
    plain = format_summary(NOW, [])
    assert "Находок нет." in plain and "Осталось" not in plain and "Покрыто" not in plain


def test_split_message_chunks_and_tags():
    lines = [f'<a href="https://x.ru/{i}">row {i}</a> ' + "x" * 200 for i in range(60)]
    chunks = split_message("\n".join(lines))
    assert len(chunks) > 1 and all(len(c) <= 4096 for c in chunks)
    assert "\n".join(chunks) == "\n".join(lines)
    assert all(c.count("<a ") == c.count("</a>") for c in chunks)
    assert split_message("short") == ["short"]
    assert all(len(c) <= 100 for c in split_message("y" * 250, 100))


def test_crawl_order_never_crawled_by_prior_score():
    cats = [
        cat(1, "A", prior=10),
        cat(2, "A", prior=500),
        cat(3, "B", prior=100),
        cat(4, "B", None),  # без оценки — последним среди необойдённых
        cat(5, "C", 40),  # уже обходили: после всех необойдённых
    ]
    assert [c.id for c in crawl_order(cats, run_id=1, now=NOW)] == [2, 1, 3, 4, 5]


def test_goofish_query():
    from app.services.market_logic import goofish_query, goofish_url

    assert goofish_query("Bugaboo Dragonfly 2в1 Desert Taupe") == "Bugaboo Dragonfly Desert Taupe"
    assert "Bugaboo Dragonfly" in goofish_query("Bugaboo Dragonfly 2в1 Desert Taupe")
    assert goofish_query("Коляска детская") is None
    assert goofish_query("Pioneer DDJ-400 новый") == "Pioneer DDJ-400"
    assert goofish_query("New Original Pioneer, set 5 kg cm") == "Pioneer"
    assert goofish_query("Apple iPhone 15 Pro Max 256 Гб black") == "Apple iPhone 15 Pro"
    assert goofish_query("12345 Куртка") is None  # цифры без латинской буквы — не модель
    assert goofish_url("Pioneer DDJ-400") == "https://www.goofish.com/search?q=Pioneer+DDJ-400"
    assert goofish_url("Коляска") is None


def test_format_card_goofish_link_only_with_model():
    assert 'href="https://www.goofish.com/search?q=Sony+A7">🔎 goofish</a>' in format_card(find(title="Sony A7"))
    assert "goofish</a>" not in format_card(find(title="Коляска детская"))


def test_goofish_digits_next_to_latin_and_nothing_invented():
    from app.services.market_logic import goofish_query

    assert goofish_query("iPhone 15 Pro Max 256 Гб") == "iPhone 15 Pro Max"
    assert goofish_query("Pioneer DDJ 400 новый") == "Pioneer DDJ 400"
    assert goofish_query("Куртка 52 размер Nike") == "Nike"  # 52 не рядом с латинским словом
    assert goofish_query("Canyon Endurace 2019 XL") == "Canyon Endurace XL"  # год не модель
    assert goofish_query("iPhone 15") == "iPhone 15" and "Apple" not in goofish_query("iPhone 15 Pro")


REAL = (
    "Aimiko u2 pro premium 3000w",
    "Aimiko U2 Pro 63V/65Ah 9a Strong",
    "Aimiko u2 PRO 3000w",
    "Aimiko u2 premium 3000w",
)


def test_model_key_groups_all_four_real_aimiko_titles():
    from app.services.market_logic import model_key

    assert {model_key(t) for t in REAL} == {"aimiko u2"}
    assert model_key("Apple iPhone 15 Pro Max") == "apple iphone 15"
    assert model_key("Apple iPhone 14") == "apple iphone 14"  # разные поколения не склеиваются
    assert model_key("iPhone 15 Pro Max") == "iphone 15 pro"
    assert model_key("Pioneer DDJ 400") == "pioneer ddj 400"
    assert model_key("Wenbox U5") == "wenbox u5"
    assert model_key("Коляска детская") is None
    assert model_key("Колонка JBL") is None  # одно слово без цифры — слишком общий ключ
    assert model_key("Doona X почти новая коляска") is None


def test_model_groups_and_format():
    from app.services.market_logic import format_models, model_groups

    rows = [
        NS(title=t, vpd=v, price_min=p, price_max=p, url=f"https://www.avito.ru/x_{i}", external_id=str(i))
        for i, (t, v, p) in enumerate(zip(REAL, (535, 411, 134, 63), (70000, 49900, 120000, 120000), strict=True), 1)
    ] + [NS(title="Wenbox U5", vpd=95, price_min=1, price_max=1, url="u", external_id="9")]
    (g,) = model_groups(rows)  # Wenbox встретился один раз — не модель с несколькими объявлениями
    assert (g.key, g.count, g.vpd_min, g.vpd_max, g.price_min, g.price_max) == ("aimiko u2", 4, 63, 535, 49900, 120000)
    assert g.best.external_id == "1"
    lines = format_models([g])
    assert lines[0].startswith("<b>🔁 Модели") and "aimiko u2 ×4 · 63–535/день · 49 900–120 000 ₽" in lines[1]
    assert 'href="https://www.avito.ru/x_1">лучшее</a>' in lines[1] and "🔎 goofish" in lines[1]
    assert format_models([]) == []
    dup = [rows[0], NS(**{**rows[0].__dict__})]  # одно объявление, найденное дважды
    assert model_groups(dup) == []


def test_margin_and_weight():
    from app.services.market_logic import calc_margin, format_margin, weight_kg

    assert weight_kg("Bugaboo коляска", "") == 12 and weight_kg("Aimiko электровелосипед") == 28
    assert weight_kg("Cube велосипед") == 15 and weight_kg("Yaesu рация") == 1 and weight_kg("Pioneer DJ DDJ-400") == 6
    assert weight_kg("Нечто") == 3 and weight_kg("adjust") == 3  # «dj» только отдельным словом
    m = calc_margin(49000, 2300, 12, 12.2, 500, 3000)
    assert m.cost == round(2300 * 12.2 + 12 * 500) == 34060
    assert (m.margin, m.pct) == (14940, 30) and m.air_margin == 49000 - round(2300 * 12.2 + 36000)
    text = format_margin(m, 49000, 12.2, 500)
    assert "себест. ~34 060 ₽ (¥2300×12.2 + доставка 12 кг×500)" in text and "маржа ~14 940 ₽ (30%)" in text
    assert calc_margin(0, 100, 1, 12.2, 500, 3000).pct == 0


def test_find_line_shows_id_when_known():
    assert format_card(find(id=123)).endswith(" · #123") and "#" not in format_card(find())


def test_summary_includes_models_block():
    text = format_summary(NOW, ["l"], models=["<b>🔁 Модели</b>", "m"])
    assert text.split("\n\n")[1:3] == ["l", "<b>🔁 Модели</b>\nm"]
