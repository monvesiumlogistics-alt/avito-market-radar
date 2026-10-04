"""Итог выборочных карточек для Telegram (ADR-025): что стоит исследовать и что нужно подтвердить. 0 загрузок."""

import html
from datetime import datetime
from urllib.parse import quote

from sqlalchemy.orm import Session

from app.db import Category
from app.services.attention import Attention, baseline, main_category, product_attention
from app.services.candidates import Candidate, run
from app.services.market_logic import pack_messages

ORDER = {"MEASURED": 0, "ATTENTION_ANOMALY": 1, "SINGLE": 2, "BASELINE_PENDING": 3, "NO_OBS": 4}


def evaluate(db: Session, now: datetime) -> tuple[list[tuple[Candidate, Attention, str]], dict]:
    """Кандидаты Phase 3 + внимание по карточкам, отсортировано: подтверждённое → аномалии → прочее."""
    cands = run(db, now)["candidates"]
    bases, out = {}, []
    for c in cands:
        cid = main_category(db, c.metrics.product_id)
        if cid not in bases:
            bases[cid] = baseline(db, cid) if cid else None
        cat = db.get(Category, cid).name if cid else "—"
        out.append((c, product_attention(db, c.metrics.product_id, bases[cid]), cat))
    out.sort(key=lambda r: (ORDER[r[1].status], -(r[1].multiple or 0), -(r[1].vpd_median or 0)))
    names = {cid: db.get(Category, cid).name for cid in bases if cid}
    return out, {names[cid]: b for cid, b in bases.items() if cid and b and b.n}


def _k(v) -> str:
    return f"{v // 1000}k" if v else "—"


def hunter_texts(db: Session, now: datetime, limit: int = 8) -> list[str]:
    rows, bases = evaluate(db, now)
    strong = [r for r in rows if r[1].status == "MEASURED" and r[1].multiple >= 1.5]
    confirm = [
        r for r in rows if r[1].status == "ATTENTION_ANOMALY" or (r[1].status == "SINGLE" and r[1].multiple >= 1.5)
    ]
    weak = [r for r in rows if r[1].multiple is not None and r[1].multiple < 0.8]
    pending = [r for r in rows if r[1].status == "BASELINE_PENDING"]
    blocks = [f"<b>🔥 Что реально стоит исследовать · {now:%d.%m}</b>"]
    norms = " · ".join(f"{html.escape(n)}: {b.median:.0f}/д (n={b.n}{'' if b.usable else ', мало'})"
                       for n, b in bases.items())  # fmt: skip
    blocks.append("Норма просмотров: " + (norms or "ещё нет"))
    if strong:
        lines = []
        for i, (c, a, cat) in enumerate(strong[:limit], 1):
            m = c.metrics
            gf = f"https://www.goofish.com/search?q={quote(m.name)}"
            lines.append(
                f"{i}. <b>{html.escape(m.name)}</b> — {html.escape(cat)}\n"
                f"Avito {_k(m.price_p25)}–{_k(m.price_p75)} · объявлений 14д {m.listings_14d}"
                f" · городов {m.cities_14d}\n"
                f"Карточек {a.obs_n} · просмотры {a.vpd_median:.0f}/д · норма {a.baseline.median:.0f} · "
                f"<b>{a.multiple:.1f}×</b> · {a.confidence}\n"
                f'<a href="{gf}">→ проверить goofish</a>'
            )
        blocks.append("\n\n".join(lines))
    else:
        blocks.append("Подтверждённых (≥ 2 объявления и ≥ 1.5× нормы) пока нет.")
    if confirm:
        blocks.append("<b>⚡ Нужно подтвердить</b> (одна сильная карточка)\n" + "\n".join(
            f"• {html.escape(c.metrics.name)} — {a.multiple:.1f}× ({a.vpd_median:.0f}/д)"
            for c, a, _ in confirm[:limit]))  # fmt: skip
    tail = []
    if weak:
        tail.append(f"Ниже рынка после просмотров: {', '.join(html.escape(c.metrics.name) for c, _, _ in weak[:6])}")
    if pending:
        tail.append(f"Без нормы категории: {len(pending)} товаров")
    if tail:
        blocks.append("<blockquote expandable>" + "\n".join(tail) + "</blockquote>")
    return pack_messages(blocks)
