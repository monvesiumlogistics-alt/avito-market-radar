"""Команды управления рынком, которым не нужен Avito: /top, /export, /cats, /skip, /unskip, 👍/👎. Только БД."""

import csv
import html
import io
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import Settings
from app.db import Category, Find
from app.services.market_logic import (
    Entry,
    calc_margin,
    format_card,
    format_line,
    format_margin,
    format_results,
    goofish_url,
    model_counts,
    model_groups,
    model_key,
    section_name,
    split_message,
    weight_kg,
)

TOP_LIMIT = 15


def _window(db: Session, days: int, now: datetime) -> list[Find]:
    return list(db.scalars(select(Find).where(Find.created_at >= now - timedelta(days=days)).order_by(Find.vpd.desc())))


def top_finds(db: Session, days: int, now: datetime, limit: int = TOP_LIMIT) -> list[Find]:
    """Лучшие находки за days дней: по group_key остаётся лучшая по vpd, сортировка по vpd, не больше limit."""
    best: dict[str, Find] = {}
    for f in _window(db, days, now):  # уже по убыванию vpd: первая в группе лучшая
        best.setdefault(f.group_key, f)
    return sorted(best.values(), key=lambda f: -f.vpd)[:limit]


def margin_text(f: Find, category: str, s: Settings) -> str | None:
    """Строка маржи, если известна цена в Китае (/price)."""
    if not f.china_price:
        return None
    kg = weight_kg(f.title, category)
    m = calc_margin(f.price_min, f.china_price, kg, s.cny_rate, s.cargo_rub_per_kg, s.cargo_air_rub_per_kg)
    return format_margin(m, f.price_min, s.cny_rate, s.cargo_rub_per_kg)


def entries_for(
    db: Session,
    finds: list[Find],
    s: Settings,
    seen: dict[str, datetime] | None = None,
    all_finds: list[Find] | None = None,
) -> list[Entry]:
    """Карточки находок с разделом и подкатегорией (для группировки). all_finds — откуда считать «модель N раз»."""
    cats = {c.id: c for c in db.scalars(select(Category).where(Category.id.in_({f.category_id for f in finds})))}
    counts = model_counts(all_finds or finds)
    out = []
    for f in finds:
        cat = cats.get(f.category_id)
        name = cat.name if cat else "?"
        card = format_card(
            f,
            (seen or {}).get(f.group_key),
            vpd_hot=s.vpd_hot,
            min_price=s.min_price,
            model_count=counts.get(model_key(f.title) or "", 1),
            margin=margin_text(f, name, s),
        )
        line = format_line(f, f.group_key in (seen or {}), margin_short(f, name, s))
        out.append(Entry(f, cat.section if cat else "", name, card, line))
    return out


def margin_short(f: Find, category: str, s: Settings) -> str | None:
    """Маржа для строки итога: «💱 ~14 940 ₽ (30%)», если известна цена в Китае."""
    if not f.china_price:
        return None
    m = calc_margin(
        f.price_min, f.china_price, weight_kg(f.title, category), s.cny_rate, s.cargo_rub_per_kg, s.cargo_air_rub_per_kg
    )
    return f"💱 ~{m.margin:,} ₽ ({m.pct}%)".replace(",", " ")


def top_messages(db: Session, days: int, now: datetime, s: Settings | None = None) -> list[str]:
    finds = top_finds(db, days, now)
    if not finds:
        return [f"За {days} дн находок нет."]
    s = s or Settings(_env_file=None)
    window = _window(db, days, now)
    entries = entries_for(db, finds, s, all_finds=window)
    stats = f"🎯 {len(finds)} находок · 🔥 {sum(bool(f.hot) for f in finds)}"
    return format_results(f"<b>📊 Топ находок · {days} дн</b>", stats, entries, model_groups(window))


def set_price(db: Session, find_id: int, yuan: int, s: Settings) -> str | None:
    """/price <id> <юани>: запомнить цену в Китае и показать маржу; None — находки нет."""
    f = db.get(Find, find_id)
    if f is None:
        return None
    f.china_price = yuan
    db.commit()
    cat = db.get(Category, f.category_id)
    return margin_text(f, cat.name if cat else "", s)


CSV_HEADER = (
    "date", "section", "category", "title", "price_min", "price_max", "vpd", "today", "age_days", "copies", "hot",
    "url", "goofish_url",
)  # fmt: skip


def _cell(text: str) -> str:
    """Ячейка Excel: текст из Avito не должен стать формулой."""
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


def export_csv(db: Session) -> bytes:
    """Все находки: CSV для Excel (utf-8-sig, разделитель ';')."""
    cats = {c.id: c for c in db.scalars(select(Category))}
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(CSV_HEADER)
    for f in db.scalars(select(Find).order_by(Find.created_at.desc(), Find.vpd.desc())):
        cat = cats.get(f.category_id)
        w.writerow([
            f"{f.created_at:%Y-%m-%d %H:%M}", cat.section if cat else "", _cell(cat.name) if cat else "",
            _cell(f.title), f.price_min, f.price_max, f.vpd, "" if f.today is None else f.today,
            round(f.age_days, 1), f.copies, int(f.hot), f.url, goofish_url(f.title) or "",
        ])  # fmt: skip
    return buf.getvalue().encode("utf-8-sig")


def cats_text(db: Session) -> list[str]:
    """Разделы: название, подкатегорий, из них пропущено."""
    sections: dict[str, list[Category]] = {}
    for c in db.scalars(select(Category).order_by(Category.section, Category.id)):
        sections.setdefault(c.section, []).append(c)
    if not sections:
        return ["Категорий пока нет: запусти /report или scripts.import_map."]
    lines = ["<b>Разделы</b> (подкатегорий / пропущено)"]
    for sec, cs in sections.items():
        skipped = sum(bool(c.skipped) for c in cs)
        mark = "⛔ " if skipped == len(cs) else ""
        lines.append(f"{mark}{html.escape(section_name(sec))} <code>{html.escape(sec)}</code>: {len(cs)} / {skipped}")
    return split_message("\n".join(lines))


def set_skipped(db: Session, text: str, value: bool) -> int:
    """Категории, у которых slug раздела или название (раздела/подкатегории) содержит text, без учёта регистра."""
    needle = text.strip().lower()
    if len(needle) < 2:  # одна буква совпала бы почти со всем
        return 0
    n = 0
    for c in db.scalars(select(Category)):
        hay = f"{c.section} {section_name(c.section)} {c.name}".lower()
        if needle in hay:
            c.skipped = value
            n += 1
    db.commit()
    return n


def set_feedback(db: Session, find_id: int, value: int) -> bool:
    """👍 (+1) / 👎 (-1) для находки; повторное нажатие перезаписывает. False — находки нет."""
    f = db.get(Find, find_id)
    if f is None or value not in (1, -1):
        return False
    f.feedback = value
    db.commit()
    return True
