"""Чистая логика проверки рынка: возраст, группы, отбор карточек, фильтры, порядок обхода. Без I/O."""

import html
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from itertools import groupby
from typing import NamedTuple
from urllib.parse import quote_plus

from app.config import Settings
from app.db import Category
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


def feedback_factor(net: int) -> float:
    """Чистые 👍 минус 👎 по находкам подкатегории -> множитель приоритета: +50 % за каждый 👍, от x0.25 до x2.5."""
    return min(max(1 + 0.5 * net, 0.25), 2.5)


def _priority(cat: Category, now: datetime, net: int = 0) -> float:
    k = feedback_factor(net)
    if cat.last_crawled_at is None:
        return NEVER_CRAWLED + max(cat.prior_score or 0, 1) * k
    days = (now - cat.last_crawled_at).total_seconds() / 86400
    return days * (1 + min(cat.last_best_vpd or 0, 500) / 100) * k


def crawl_order(
    cats: Sequence[Category], run_id: int, now: datetime, feedback: Mapping[int, int] | None = None
) -> list[Category]:
    """Непройденные в этом прогоне, без /skip; разделы по лучшему приоритету подкатегорий, внутри — по приоритету.

    feedback: category_id -> чистые 👍/👎; 👍 поднимают подкатегорию, 👎 опускают."""
    fb = feedback or {}

    def prio(c) -> float:
        return _priority(c, now, fb.get(c.id, 0))

    todo = sorted(
        (c for c in cats if c.last_run_id != run_id and not getattr(c, "skipped", False)),
        key=lambda c: (-prio(c), c.id),
    )
    best: dict[str, float] = {}
    for c in todo:  # todo по убыванию: первая подкатегория раздела — лучшая
        best.setdefault(c.section, prio(c))
    rank = {sec: i for i, sec in enumerate(sorted(best, key=lambda sec: -best[sec]))}
    return sorted(todo, key=lambda c: rank[c.section])  # sorted стабилен: внутри раздела порядок по приоритету


GOOFISH_STOP = {"new", "original", "orig", "size", "cm", "mm", "kg", "set", "lot"}  # pro/max/mini — части моделей
GOOFISH_MAX_TOKENS = 4
_LATIN_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9.\-]*")


_DIGITS = re.compile(r"\d+(?:\.\d+)?")
_YEAR = re.compile(r"(?:19|20)\d\d")
_EDGE = ".,;:!?()[]{}\"'«»-"


def _model_tokens(title: str) -> list[tuple[str, bool]]:
    """Токены модели по порядку: (токен, is_latin). Латинские слова/коды (>= 2 символов, хотя бы одна буква, не из
    GOOFISH_STOP) и числа рядом с таким словом («iPhone 15», «DDJ 400»); годы не берём. Ничего не добавляем от себя."""
    raw = [t.strip(_EDGE) for t in title.split()]
    latin = [
        len(t) >= 2
        and bool(_LATIN_TOKEN.fullmatch(t))
        and bool(re.search(r"[A-Za-z]", t))
        and t.lower() not in GOOFISH_STOP
        for t in raw
    ]
    out: list[tuple[str, bool]] = []
    for i, t in enumerate(raw):
        if latin[i]:
            out.append((t, True))
        elif (
            _DIGITS.fullmatch(t)
            and not _YEAR.fullmatch(t)
            and ((i > 0 and latin[i - 1]) or (i + 1 < len(raw) and latin[i + 1]))
        ):
            out.append((t, False))
    return out


def goofish_query(title: str) -> str | None:
    """Латинские токены названия (бренд + модель, числа рядом с ними) для поиска на goofish; None без модели."""
    return " ".join(t for t, _ in _model_tokens(title)[:GOOFISH_MAX_TOKENS]) or None


def model_key(title: str) -> str | None:
    """Ключ модели для склейки объявлений разных продавцов: первые 2 латинских токена (+ числа между/после них),
    в нижнем регистре. «Aimiko u2 pro 3000w» и «Aimiko U2 Pro 63V/65Ah» -> «aimiko u2» (версии/мощность в ключ
    не входят, их разброс виден в диапазонах vpd и цены). Консервативно: нужен токен с цифрой или 2 латинских слова."""
    key: list[str] = []
    latin = 0
    for tok, is_latin in _model_tokens(title):
        if latin == 2:
            if not is_latin:
                key.append(tok)  # число сразу после второго слова: «apple iphone 15»
            break
        key.append(tok)
        latin += is_latin
    if not key or (latin < 2 and not any(c.isdigit() for t in key for c in t)):
        return None
    return " ".join(key).lower()


@dataclass
class ModelGroup:
    key: str
    count: int
    vpd_min: int
    vpd_max: int
    price_min: int
    price_max: int
    best: object  # находка с максимальным vpd
    gone: int = 0  # сколько из объявлений уже ушло


def model_groups(finds: Sequence) -> list[ModelGroup]:
    """Модели, встретившиеся >= 2 раз (разные объявления: external_id), по убыванию count x max vpd."""
    by_key: dict[str, dict[str, object]] = {}
    for f in finds:
        if key := model_key(f.title):
            by_key.setdefault(key, {})[getattr(f, "external_id", f.title)] = f
    groups = []
    for key, items in by_key.items():
        fs = list(items.values())
        if len(fs) < 2:
            continue
        best = max(fs, key=lambda f: f.vpd)
        groups.append(
            ModelGroup(
                key, len(fs), min(f.vpd for f in fs), best.vpd, min(f.price_min for f in fs),
                max(f.price_max for f in fs), best,
                sum(bool(getattr(f, "gone_at", None)) for f in fs),
            )
        )  # fmt: skip
    return sorted(groups, key=lambda g: -g.count * g.vpd_max)


# --- маржа ---

WEIGHT_PRESETS = (  # (регулярка по названию + категории в нижнем регистре, кг); первое совпадение, порядок важен
    (re.compile(r"электровелосипед|электро-велосипед|e-?bike"), 28.0),
    (re.compile(r"велосипед"), 15.0),
    (re.compile(r"коляск"), 12.0),
    (re.compile(r"автокресл"), 10.0),
    (re.compile(r"самокат"), 15.0),
    (re.compile(r"рации|рация|радиостанц"), 1.0),
    (re.compile(r"телефон|смартфон"), 0.5),
    (re.compile(r"часы"), 0.3),
    (re.compile(r"\bdj\b|контроллер"), 6.0),
)
DEFAULT_WEIGHT_KG = 3.0


def weight_kg(*texts: str) -> float:
    hay = " ".join(texts).lower()
    return next((kg for rx, kg in WEIGHT_PRESETS if rx.search(hay)), DEFAULT_WEIGHT_KG)


@dataclass(frozen=True)
class Margin:
    yuan: int
    kg: float
    cost: int  # себестоимость: товар + наземка
    margin: int
    pct: int
    air_margin: int
    air_pct: int


def calc_margin(price_rub: int, yuan: int, kg: float, rate: float, ground_per_kg: float, air_per_kg: float) -> Margin:
    """Продажа по цене объявления (price_rub) минус ¥ x курс минус доставка весом kg. pct = маржа / продажа."""
    goods = yuan * rate
    cost, air_cost = goods + kg * ground_per_kg, goods + kg * air_per_kg
    pct = lambda m: round(100 * m / price_rub) if price_rub else 0  # noqa: E731
    return Margin(
        yuan,
        kg,
        round(cost),
        round(price_rub - cost),
        pct(price_rub - cost),
        round(price_rub - air_cost),
        pct(price_rub - air_cost),
    )


def format_margin(m: Margin, price_rub: int, rate: float, ground_per_kg: float) -> str:
    kg = f"{m.kg:g}"
    return (
        f"себест. ~{_rub(m.cost)} ₽ (¥{m.yuan}×{rate:g} + доставка {kg} кг×{ground_per_kg:g}),"
        f" продажа ~{_rub(price_rub)} ₽"
        f" → маржа ~{_rub(m.margin)} ₽ ({m.pct}%); авиа: ~{_rub(m.air_margin)} ₽ ({m.air_pct}%)"
    )


def goofish_url(title: str) -> str | None:
    q = goofish_query(title)
    return f"https://www.goofish.com/search?q={quote_plus(q)}" if q else None


def _rub(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def _times(n: int) -> str:
    return "раз" if n % 10 in (0, 1) or n % 10 >= 5 or 11 <= n % 100 <= 14 else "раза"


def gone_days(f) -> int | None:
    """За сколько дней находка ушла (дней от находки до исчезновения, не меньше 1); None — не ушла/неизвестно."""
    gone_at = getattr(f, "gone_at", None)
    return max(round((gone_at - f.created_at).total_seconds() / 86400), 1) if gone_at else None


SECTION_EMOJI = {
    "telefony": "📱", "audio_i_video": "🎧", "tovary_dlya_kompyutera": "🖱", "noutbuki": "💻",
    "nastolnye_kompyutery": "🖥", "planshety_i_elektronnye_knigi": "📲", "orgtehnika_i_rashodniki": "🖨",
    "fototehnika": "📷", "igry_pristavki_i_programmy": "🎮", "bytovaya_tehnika": "🏠",
    "odezhda_obuv_aksessuary": "👕", "detskaya_odezhda_i_obuv": "👶", "tovary_dlya_detey_i_igrushki": "🧸",
    "chasy_i_ukrasheniya": "⌚", "krasota_i_zdorove": "💄", "remont_i_stroitelstvo": "🔧",
    "mebel_i_interer": "🛋", "posuda_i_tovary_dlya_kuhni": "🍳", "kollektsionirovanie": "🪙",
    "muzykalnye_instrumenty": "🎛", "ohota_i_rybalka": "🎣", "sport_i_otdyh": "⚽", "velosipedy": "🚲",
    "tovary_dlya_zhivotnyh": "🐾", "zapchasti_i_aksessuary": "🚗",
}  # fmt: skip
SHORT_TITLE = 60
SHORT_LINE_TITLE = 40  # название в строке итога
MAX_REASONS = 3


def section_emoji(slug: str) -> str:
    return SECTION_EMOJI.get(slug, "📦")


def reason(f, vpd_hot: int = 100, min_price: int = 10000, model_count: int = 1) -> str:
    """Почему находка: 1-3 коротких довода по приоритету (ушло, спрос, копии, модель, свежесть, бренд, чек)."""
    out = []
    if gone := gone_days(f):
        out.append(f"✅ ушло за {gone} дн — реально покупают")
    out.append(f"очень высокий спрос: {f.vpd} просм/день" if f.vpd >= vpd_hot else f"спрос {f.vpd} просм/день")
    if f.copies >= 2:
        out.append(f"выставлено {f.copies} {_times(f.copies)} — товар ходовой")
    if model_count >= 2:
        out.append(f"модель встречается {model_count} {_times(model_count)}")
    if f.age_days <= 1:
        out.append("свежее (≤1 дн)")
    if goofish_query(f.title):
        out.append("есть бренд/модель — легко найти на goofish")
    if f.price_min >= min_price * 3:
        out.append("дорогой чек — маржа в рублях выше")
    return "; ".join(out[:MAX_REASONS])


def format_card(
    f,
    seen_on: datetime | None = None,
    *,
    vpd_hot: int = 100,
    min_price: int = 10000,
    model_count: int = 1,
    margin: str | None = None,
) -> str:
    """Карточка находки, 3-5 строк (резать можно только между карточками, см. split_message)."""
    title = html.escape(f.title[:SHORT_TITLE]) + ("…" if len(f.title) > SHORT_TITLE else "")
    head = f'{"🔥 " if f.hot else ""}<a href="{html.escape(f.url)}">{title}</a>' + (
        f" ×{f.copies}" if f.copies > 1 else ""
    )
    price = f"{_rub(f.price_min)} ₽" if f.price_min == f.price_max else f"{_rub(f.price_min)}–{_rub(f.price_max)} ₽"
    views = f"{f.vpd}/день"
    detail = [f"+{f.today} сегодня"] if f.today is not None else []
    total = getattr(f, "views", None)
    if total is not None:
        detail.append(f"всего {total}")
    if detail:
        views += f" ({', '.join(detail)})"
    day = getattr(f, "seller_date", None) or getattr(f, "page_date", None)
    age = f"{max(round(f.age_days), 1)} дн"
    when = f"{day:%d.%m} ({age})" if day else f"~{age}"
    if not f.date_checked:
        when = f"{day:%d.%m} (дата не проверена)" if day else "дата не проверена"
    info = f"💰 {price} · 👁 {views} · 📅 {when}"
    if seen_on:
        info += f" · уже было {seen_on:%d.%m}"
    lines = [head, info, "💡 " + reason(f, vpd_hot, min_price, model_count)]
    if margin:
        lines.append("💱 " + margin)
    tail = []
    if goofish := goofish_url(f.title):
        tail.append(f'<a href="{html.escape(goofish)}">🔎 goofish</a>')
    if (find_id := getattr(f, "id", None)) is not None:
        tail.append(f"#{find_id}")  # для /price <id> <юани>
    if tail:
        lines.append(" · ".join(tail))
    return "\n".join(lines)


def model_counts(finds: Sequence) -> dict[str, int]:
    """Сколько разных объявлений у каждой модели (для довода «модель встречается N раз»)."""
    seen: dict[str, set] = {}
    for f in finds:
        if key := model_key(f.title):
            seen.setdefault(key, set()).add(getattr(f, "external_id", f.title))
    return {k: len(v) for k, v in seen.items()}


class Entry(NamedTuple):
    find: object
    section: str
    sub: str
    card: str  # полная карточка (для 🔥 сообщения)
    line: str = ""  # одна строка для итога/«/top» (формат D)


def short_title(title: str, cap: int = SHORT_LINE_TITLE) -> str:
    return html.escape(title[:cap]) + ("…" if len(title) > cap else "")


def format_line(f, seen: bool = False, margin: str | None = None) -> str:
    """Строка находки в итоге (формат D): 🔥 название ×N · цена · vpd/д · ушло · уже было · 🔎 · #id."""
    head = f"{'🔥 ' if f.hot else ''}<a href=\"{html.escape(f.url)}\">{short_title(f.title)}</a>"
    if f.copies > 1:
        head += f" ×{f.copies}"
    price = f"{_rub(f.price_min)} ₽" if f.price_min == f.price_max else f"{_rub(f.price_min)}–{_rub(f.price_max)} ₽"
    parts = [head, price, f"{f.vpd}/д"]
    if gone := gone_days(f):
        parts.append(f"✅ ушло за {gone} дн")
    if seen:
        parts.append("уже было")
    if margin:
        parts.append(margin)
    if goofish := goofish_url(f.title):
        parts.append(f'<a href="{html.escape(goofish)}">🔎</a>')
    if (find_id := getattr(f, "id", None)) is not None:
        parts.append(f"#{find_id}")
    return " · ".join(parts)


def group_sections(entries: Sequence[Entry]) -> list[tuple[str, list[Entry]]]:
    """По разделам: внутри по vpd, разделы по лучшему vpd."""
    by: dict[str, list[Entry]] = {}
    for e in entries:
        by.setdefault(e.section, []).append(e)
    out = [(sec, sorted(es, key=lambda e: -e.find.vpd)) for sec, es in by.items()]
    return sorted(out, key=lambda p: -p[1][0].find.vpd)


QUOTE_OPEN, QUOTE_CLOSE = "<blockquote expandable>", "</blockquote>"


def quote_blocks(header: str, cont_header: str, lines: Sequence[str], limit: int = TG_LIMIT) -> list[str]:
    """Заголовок + раскрывающаяся цитата со строками; не влезает в limit — несколько цитат с «(продолжение)»,
    внутри тега не режем никогда."""
    blocks: list[str] = []
    cur: list[str] = []
    head = header

    def build() -> str:
        return f"{head}\n{QUOTE_OPEN}{chr(10).join(cur)}{QUOTE_CLOSE}"

    for line in lines:
        cur.append(line)
        if len(cur) > 1 and len(build()) > limit:
            cur.pop()
            blocks.append(build())
            head, cur = cont_header, [line]
    blocks.append(build())
    return blocks


def pack_messages(blocks: Sequence[str], limit: int = TG_LIMIT) -> list[str]:
    msgs: list[str] = []
    cur = ""
    for b in blocks:
        if cur and len(cur) + 2 + len(b) > limit:
            msgs.append(cur)
            cur = b
        else:
            cur = f"{cur}\n\n{b}" if cur else b
    if cur:
        msgs.append(cur)
    return msgs


def format_model_lines(groups: Sequence[ModelGroup]) -> list[str]:
    out = []
    for g in groups:
        vpd = f"{g.vpd_min}" if g.vpd_min == g.vpd_max else f"{g.vpd_min}–{g.vpd_max}"
        parts = [f"{html.escape(g.key)} ×{g.count}", f"{vpd}/д"]
        if g.gone:
            parts.append(f"✅ ушло {g.gone}")
        parts.append(f'<a href="{html.escape(g.best.url)}">лучшее</a>')
        if gf := goofish_url(g.best.title):
            parts.append(f'<a href="{html.escape(gf)}">🔎</a>')
        out.append(" · ".join(parts))
    return out


def format_results(
    title: str,
    stats: str,
    entries: Sequence[Entry],
    models: Sequence[ModelGroup] = (),
    notes: Sequence[str] = (),
    tail: Sequence[str] = (),
    limit: int = TG_LIMIT,
) -> list[str]:
    """Итог (формат D) для прогона и /top: шапка, раздел = заголовок + раскрывающаяся цитата, блок моделей, хвост.
    Возвращает сообщения не длиннее limit; цитаты не режутся."""
    head = "\n".join([title, stats, *notes, *([] if entries else ["Находок нет."])])
    blocks = [head]
    for sec, es in group_sections(entries):
        name = f"{section_emoji(sec)} {html.escape(section_name(sec))}"
        lines = [e.line for e in es]
        blocks += quote_blocks(f"<b>{name} — {len(es)}</b>", f"<b>{name} (продолжение)</b>", lines, limit)
    if models:
        blocks += quote_blocks(
            f"<b>🔁 Модели с несколькими объявлениями — {len(models)}</b>",
            "<b>🔁 Модели (продолжение)</b>",
            format_model_lines(models),
            limit,
        )
    if tail:
        blocks.append("\n".join(tail))
    return pack_messages(blocks, limit)


def summary_tail(
    covered: Sequence[tuple[str, float]], errors: Sequence[str], remaining: int, max_age_days: int
) -> list[str]:
    """Хвост итога: покрытие, ошибки, сколько осталось (коротко, одним блоком)."""
    tail = []
    if covered:
        tail.append(
            "Покрыто не полностью: "
            + ", ".join(f"{html.escape(n)} — {round(d, 1):g} из {max_age_days} дн" for n, d in covered)
        )
    tail += [f"ошибка: {html.escape(e[:200])}" for e in errors[:MAX_ERRORS]]
    if len(errors) > MAX_ERRORS:
        tail.append(f"и ещё {len(errors) - MAX_ERRORS}")
    if remaining:
        tail.append(f"Осталось {remaining} подкатегорий, пойдут первыми в следующий раз.")
    return tail


FINAL_HEADERS = {
    "done": "✅ Проверка завершена",
    "budget": "✅ Проверка завершена (бюджет загрузок)",
    "stopped": "⏹ Остановлено",
    "blocked": "⚠️ Блок Avito",
    "failed": "⚠️ Проверка упала",
    "interrupted": "⏹ Прервано",
}
BAR_WIDTH = 22
ETA_MIN_LOADS = 10  # раньше оценка скорости слишком шумная
# Премиум-иконки набора t.me/addemoji/UnigramIcons: обычный эмодзи -> custom_emoji_id (PREMIUM_EMOJI=true).
PREMIUM_ICONS = {
    "🔎": "5870974879200711167",
    "📂": "5870570722778156940",
    "ℹ️": "5870609858520158157",
    "⏩": "5870934523687997110",
    "✅": "5870633910337015697",
    "❗️": "5870931487146119264",
    "⏲": "5870496192210669260",
    "📖": "5870729937215819584",
    "▶": "5870921127685001066",
    "⚠️": "5872988737826197458",
}


def icon(emoji: str, premium: bool = False) -> str:
    cid = PREMIUM_ICONS.get(emoji) if premium else None
    return f'<tg-emoji emoji-id="{cid}">{emoji}</tg-emoji>' if cid else emoji


def _mmss(seconds: float) -> str:
    total = max(int(seconds), 0)
    return f"{total // 60:02d}:{total % 60:02d}"


def progress_bar(loads: int, budget: int) -> str:
    done = min(max(round(BAR_WIDTH * loads / budget), 0), BAR_WIDTH) if budget else 0
    return "▰" * done + "▱" * (BAR_WIDTH - done)


def current_label(section: str, sub: str) -> str:
    return f"{section_emoji(section)} {html.escape(section_name(section))} — {html.escape(sub)}"


def format_progress(
    elapsed: float,
    loads: int,
    budget: int,
    subcats: int,
    finds: int,
    hot: int = 0,
    current: str | None = None,
    status: str | None = None,
    total: int | None = None,
    errors: int = 0,
    premium: bool = False,
) -> str:
    """Одно живое сообщение: заголовок, что обходится, полоса с %, статистика построчно.
    status=None — идёт (с «Осталось» и текущей категорией); иначе итоговый заголовок.
    premium — иконки UnigramIcons вместо обычных эмодзи (боту нужен username с Fragment)."""

    def i(e: str) -> str:
        return icon(e, premium)

    head = "🔎 Проверка рынка" if status is None else FINAL_HEADERS.get(status, "⏹ Остановлено")
    lead, _, rest = head.partition(" ")
    head = f"{i(lead)} {rest}"
    pct = min(100 * loads / budget, 100) if budget else 0
    left = "—"
    if status is None and loads >= ETA_MIN_LOADS and budget > loads:
        left = f"~{max(round(elapsed / loads * (budget - loads) / 60), 1)} мин"
    speed = f"{loads / (elapsed / 60):.1f} стр/мин" if elapsed >= 30 and loads else "—"
    lines = [f"<b>{head}</b>", ""]
    if status is None and current:
        lines.append(f"{i('📂')} {current}")
    lines += [
        f"<code>{progress_bar(loads, budget)}  {pct:.1f}%</code>",
        "",
        f"<b>{i('ℹ️')} {'Обход…' if status is None else 'Готово'}</b>",
        f"{i('⏩')} Страниц: <code>{loads} / {budget}</code>",
        f"{i('✅')} Найдено: <code>{finds}</code>" + (f" · 🔥 <code>{hot}</code>" if hot else ""),
        f"{i('❗️')} Ошибок: <code>{errors}</code>",
        f"{i('⏲')} Скорость: <code>{speed}</code>",
        f"{i('📖')} Прошло: <code>{_mmss(elapsed)}</code> · Осталось: <code>{left}</code>",
        f"{i('▶')} Подкатегорий: <code>{subcats}" + (f" / {total}" if total else "") + "</code>",
    ]
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


def _split_lines(text: str, limit: int) -> list[str]:
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


def split_message(text: str, limit: int = TG_LIMIT) -> list[str]:
    """Блоки (разделены пустой строкой, напр. карточки) не режутся; блок длиннее limit режется по строкам, строка
    длиннее limit (бывает «Покрыто не полностью») — по «, »."""
    chunks: list[str] = []
    current = ""
    for block in text.split("\n\n"):
        if len(block) > limit:
            if current:
                chunks.append(current)
                current = ""
            chunks.extend(_split_lines(block, limit))
        elif current and len(current) + 2 + len(block) > limit:
            chunks.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}" if current else block
    if current:
        chunks.append(current)
    return chunks
