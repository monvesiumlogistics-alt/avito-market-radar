"""Чистая логика проверки рынка: возраст, группы, отбор карточек, фильтры, порядок обхода. Без I/O."""

import html
import re
from collections.abc import Mapping, Sequence
from datetime import datetime
from itertools import groupby
from urllib.parse import quote_plus

from app.config import Settings
from app.db import Category, Find
from app.models import Listing
from app.services.matcher import normalize

STOP_WORDS = {"новый", "новая", "новое", "новые", "оригинал", "original", "new", "шт", "комплект"}
GROUP_PRICE_SPREAD = 1.2  # копии одного товара: цена до +20 % от самой дешёвой в группе
TITLE_CAP = 120  # одна строка находки никогда не упрётся в лимит Telegram
MAX_ERRORS = 10
TG_LIMIT = 4096

# Названия разделов для заголовков порций (25 slug'ов TOP_SECTIONS); неизвестный slug -> slug без «_»
SECTION_NAMES = {
    "telefony": "Телефоны",
    "audio_i_video": "Аудио и видео",
    "tovary_dlya_kompyutera": "Товары для компьютера",
    "noutbuki": "Ноутбуки",
    "nastolnye_kompyutery": "Настольные компьютеры",
    "planshety_i_elektronnye_knigi": "Планшеты и электронные книги",
    "orgtehnika_i_rashodniki": "Оргтехника и расходники",
    "fototehnika": "Фототехника",
    "igry_pristavki_i_programmy": "Игры, приставки и программы",
    "bytovaya_tehnika": "Бытовая техника",
    "odezhda_obuv_aksessuary": "Одежда, обувь, аксессуары",
    "detskaya_odezhda_i_obuv": "Детская одежда и обувь",
    "tovary_dlya_detey_i_igrushki": "Товары для детей и игрушки",
    "chasy_i_ukrasheniya": "Часы и украшения",
    "krasota_i_zdorove": "Красота и здоровье",
    "remont_i_stroitelstvo": "Ремонт и строительство",
    "mebel_i_interer": "Мебель и интерьер",
    "posuda_i_tovary_dlya_kuhni": "Посуда и товары для кухни",
    "kollektsionirovanie": "Коллекционирование",
    "muzykalnye_instrumenty": "Музыкальные инструменты",
    "ohota_i_rybalka": "Охота и рыбалка",
    "sport_i_otdyh": "Спорт и отдых",
    "velosipedy": "Велосипеды",
    "tovary_dlya_zhivotnyh": "Товары для животных",
    "zapchasti_i_aksessuary": "Запчасти и аксессуары",
}


def section_name(slug: str) -> str:
    return SECTION_NAMES.get(slug, slug.replace("_", " "))


def age_days(published_at: datetime, now: datetime) -> float:
    """Возраст в днях, не меньше 1 (знаменатель vpd)."""
    return max((now - published_at).total_seconds() / 86400, 1.0)


def find_age(page_date: datetime | None, search_date: datetime | None, now: datetime) -> float | None:
    """Возраст для vpd: дата со страницы карточки, дата выдачи — запасной вариант (m3)."""
    date = page_date or search_date
    return age_days(date, now) if date else None


def calc_vpd(views: int, age: float) -> int:
    return round(views / age)


def date_checked(seller_date: datetime | None, check_seller: bool) -> bool:
    """Отключённая проверка профиля: дата карточки и есть настоящая, пометка «не проверена» не нужна (m4)."""
    return seller_date is not None or not check_seller


def norm_title(title: str) -> str:
    """'Pioneer XDJ-RX3 новый' -> 'pioneer xdj rx3'; модели и цифры сохраняются."""
    return " ".join(w for w in normalize(title).split() if w not in STOP_WORDS)


def group_cards(cards: Sequence[Listing]) -> list[list[Listing]]:
    """Одинаковый norm_title; внутри — по цене, новая группа при цене > 1.2 × минимальной в группе."""
    groups: list[list[Listing]] = []
    priced = sorted((c for c in cards if c.price), key=lambda c: (norm_title(c.title), c.price))
    for _, same in groupby(priced, key=lambda c: norm_title(c.title)):
        current: list[Listing] = []
        for c in same:
            if current and c.price > current[0].price * GROUP_PRICE_SPREAD:
                groups.append(current)
                current = []
            current.append(c)
        groups.append(current)
    return groups


def _oldest_first(c: Listing) -> datetime:
    return c.published_at or datetime.max  # без даты — в конец


def pick_groups(
    groups: Sequence[Sequence[Listing]], per_group: int = 2
) -> list[tuple[list[Listing], Sequence[Listing]]]:
    """(что открыть, вся группа): группы с самыми старыми карточками первыми, открыть не больше per_group.

    Копии и диапазон цен в находке считаются по всей группе, а не по открытым карточкам (ADR-007).
    Слоты «по 2 на группу» не резервируются: вторую карточку краулер открывает условно и сам считает
    открытые страницы до REPORT_CARDS_PER_SUBCAT (m2).
    """
    ordered = [(sorted(g, key=_oldest_first)[:per_group], g) for g in groups if g]
    return sorted(ordered, key=lambda p: _oldest_first(p[0][0]))


def pick_cards(groups: Sequence[Sequence[Listing]], per_group: int = 2) -> list[list[Listing]]:
    return [picked for picked, _ in pick_groups(groups, per_group)]


def is_find(vpd: int, price: int, age: float, s: Settings) -> bool:
    return vpd >= s.vpd_min and price >= s.min_price and age <= s.report_max_age_days


def is_hot(vpd: int, s: Settings) -> bool:
    return vpd >= s.vpd_hot


NEVER_CRAWLED = 1e9  # выше любого «дней с обхода × вес»; внутри — по prior_score из карты


def _priority(cat: Category, now: datetime) -> float:
    if cat.last_crawled_at is None:
        return NEVER_CRAWLED + (cat.prior_score or 0)
    days = (now - cat.last_crawled_at).total_seconds() / 86400
    return days * (1 + min(cat.last_best_vpd or 0, 500) / 100)


def crawl_order(cats: Sequence[Category], run_id: int, now: datetime) -> list[Category]:
    """Непройденные в этом прогоне; разделы по лучшему приоритету подкатегорий, внутри раздела — по приоритету."""
    todo = sorted((c for c in cats if c.last_run_id != run_id), key=lambda c: (-_priority(c, now), c.id))
    best: dict[str, float] = {}
    for c in todo:  # todo по убыванию: первая подкатегория раздела — лучшая
        best.setdefault(c.section, _priority(c, now))
    rank = {sec: i for i, sec in enumerate(sorted(best, key=lambda sec: -best[sec]))}
    return sorted(todo, key=lambda c: rank[c.section])  # sorted стабилен: внутри раздела порядок по приоритету


def sort_finds(finds: Sequence, seen: Mapping[str, datetime]) -> list:
    """🔥 первыми, затем новые перед «уже было», затем по vpd. finds: .hot .vpd .group_key; seen: group_key -> дата."""
    return sorted(finds, key=lambda f: (not f.hot, f.group_key in seen, -f.vpd))


GOOFISH_STOP = {"new", "original", "orig", "size", "cm", "mm", "kg", "set", "lot"}  # pro/max/mini — части моделей
GOOFISH_MAX_TOKENS = 4
_LATIN_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-]*")


def goofish_query(title: str) -> str | None:
    """Латинские токены названия (бренд + модель) для поиска на goofish; None, если модели в названии нет."""
    tokens = []
    for raw in title.split():
        t = raw.strip(".,;:!?()[]{}\"'«»-")
        if (
            len(t) >= 2
            and _LATIN_TOKEN.fullmatch(t)
            and re.search(r"[A-Za-z]", t)
            and t.lower() not in GOOFISH_STOP
        ):
            tokens.append(t)
    return " ".join(tokens[:GOOFISH_MAX_TOKENS]) or None


def goofish_url(title: str) -> str | None:
    q = goofish_query(title)
    return f"https://www.goofish.com/search?q={quote_plus(q)}" if q else None


def _rub(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def _times(n: int) -> str:
    return "раз" if n % 10 in (0, 1) or n % 10 >= 5 or 11 <= n % 100 <= 14 else "раза"


def format_find(f: Find, category: str, seen_on: datetime | None = None) -> str:
    """Одна строка = одна группа. Каждая строка самодостаточна по тегам (резать можно по границам строк)."""
    price = f"{_rub(f.price_min)} ₽" if f.price_min == f.price_max else f"{_rub(f.price_min)}–{_rub(f.price_max)} ₽"
    views = f"{f.vpd}/день" + (f" (+{f.today} сегодня)" if f.today is not None else "")
    age = f"{max(round(f.age_days), 1)} дн" if f.date_checked else "дата не проверена"
    parts = [price, views, age, f"выставлено {f.copies} {_times(f.copies)}", html.escape(category)]
    if seen_on:
        parts.append(f"уже было {seen_on:%d.%m}")
    if goofish := goofish_url(f.title):
        parts.append(f'<a href="{html.escape(goofish)}">🔎 goofish</a>')
    link = f'<a href="{html.escape(f.url)}">{html.escape(f.title[:TITLE_CAP])}</a>'
    return f"{'🔥 ' if f.hot else ''}{link} — " + " · ".join(parts)


def format_progress(sections_done: int, sections_total: int, subcats: int, loads: int, budget: int, finds: int) -> str:
    return (
        f"⏳ Проверка рынка: разделов {sections_done}/{sections_total} · подкатегорий {subcats}"
        f" · загрузок {loads}/{budget} · находок {finds}"
    )


def format_summary(
    day: datetime,
    find_lines: Sequence[str],
    covered: Sequence[tuple[str, float]] = (),
    errors: Sequence[str] = (),
    remaining: int = 0,
    max_age_days: int = 7,
    note: str | None = None,
    totals: str | None = None,
) -> str:
    """Итог прогона. covered: (подкатегория, дней покрыто) только там, где неделя не вошла в лимит страниц."""
    lines = [
        f"<b>Итог проверки — {day:%d.%m}</b>",
        *([note] if note else []),
        *([totals] if totals else []),
        *(find_lines or ["Находок нет."]),
    ]
    if covered:
        lines.append(
            "Покрыто не полностью: "
            + ", ".join(f"{html.escape(n)} — {round(d, 1):g} из {max_age_days} дн" for n, d in covered)
        )
    lines += [f"ошибка: {html.escape(e[:200])}" for e in errors[:MAX_ERRORS]]
    if len(errors) > MAX_ERRORS:
        lines.append(f"и ещё {len(errors) - MAX_ERRORS}")
    if remaining:
        lines.append(f"Осталось {remaining} подкатегорий, пойдут первыми в следующий раз.")
    return "\n".join(lines)


def _cut_at(line: str, limit: int) -> int:
    """Где резать слишком длинную строку: после «, », иначе после пробела, иначе жёстко; не внутри тега и &entity;."""
    head = line[:limit]
    cut = limit
    for sep in (", ", " "):
        i = head.rfind(sep)
        if i > 0:
            cut = i + len(sep)
            break
    part = head[:cut]
    lt = part.rfind("<")
    if lt > part.rfind(">"):
        cut = lt
    else:
        amp = part.rfind("&")
        if amp > part.rfind(";") and len(part) - amp <= 10:
            cut = amp
    return cut or limit


def split_message(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Режет по границам строк; строка длиннее limit (бывает «Покрыто не полностью») режется по «, »."""
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        if len(line) > limit:
            if current:  # порядок сообщений: сначала накопленное
                chunks.append(current)
                current = ""
            while len(line) > limit:
                i = _cut_at(line, limit)
                chunks.append(line[:i])
                line = line[i:]
        if current and len(current) + 1 + len(line) > limit:
            chunks.append(current)
            current = line
        else:
            current = f"{current}\n{line}" if current else line
    if current:
        chunks.append(current)
    return chunks
