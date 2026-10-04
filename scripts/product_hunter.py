# ruff: noqa: E501 — форматирование отчёта
"""Офлайн-отчёт Phase 4 (ADR-025): кандидаты + замеры карточек → норма категорий → Attention. 0 загрузок Avito.

    PYTHONIOENCODING=utf-8 .venv/Scripts/python -m scripts.product_hunter [--top 20]
"""

import sys
from collections import Counter

from app.config import settings
from app.db import Category, init_db
from app.providers.avito_parser import msk_now
from app.services.attention import Attention, baseline, main_category, product_attention
from app.services.candidates import Candidate, run
from app.services.deep_scan import observation_counts

STATUS_ORDER = {"MEASURED": 0, "ATTENTION_ANOMALY": 1, "SINGLE": 2, "BASELINE_PENDING": 3, "NO_OBS": 4}
CONF = {"HIGH": 0, "MEDIUM": 1, "LOW": 2, "NONE": 3}


def rub(v):
    return f"{v // 1000}k" if v else "—"


def num(v):
    return "—" if v is None else f"{v:.0f}"


def why(c: Candidate, a: Attention) -> list[str]:
    out = []
    if "REPEATED" in c.reasons:
        out.append(f"повторяется: {c.metrics.listings_14d} объявлений за 14 дн")
    if c.metrics.cities_14d >= 3:
        out.append(f"{c.metrics.cities_14d} городов")
    if a.status == "MEASURED" and a.multiple is not None:
        out.append(f"внимание {a.multiple:.1f}× нормы категории по {a.obs_n} объявлениям" if a.multiple >= 1.5
                   else f"внимание на уровне рынка ({a.multiple:.1f}×)" if a.multiple >= 0.8
                   else f"внимание ниже рынка ({a.multiple:.1f}×)")  # fmt: skip
    if a.status == "ATTENTION_ANOMALY":
        out.append(f"одна карточка {a.multiple:.1f}× нормы — нужно подтверждение второй")
    if a.status == "BASELINE_PENDING":
        out.append(f"просмотры {num(a.vpd_median)}/д, нормы категории ещё нет")
    if "HIGH_TICKET" in c.reasons:
        out.append("чек выше 75% категории")
    return out


def block(i: int, c: Candidate, a: Attention, cats) -> str:
    m, b = c.metrics, a.baseline
    base = (f"{b.n} объявл. · медиана {num(b.median)} · p75 {num(b.p75)} · {b.confidence}" if b and b.n else "нет")
    att = f"{a.multiple:.1f}×" if a.multiple is not None else a.status
    return "\n".join([
        f"{i}. {m.name} — {cats}",
        f"   модель {m.identity} · причины: {', '.join(c.reasons)} · кандидат {c.confidence} · независимость {c.independence}",
        f"   Avito: {rub(m.price_p25)} / {rub(m.price_median)} / {rub(m.price_p75)} · объявлений 3д {m.listings_72h} · 7д {m.listings_7d} · 14д {m.listings_14d} · городов {m.cities_14d}",
        f"   карточек: {a.obs_n} · просмотры/д товара: медиана {num(a.vpd_median)} · p75 {num(a.vpd_p75)}",
        f"   норма категории: {base}",
        f"   Attention: {att} · уверенность {a.confidence} · статус {a.status}",
        "   почему: " + "; ".join(why(c, a)),
    ])


if __name__ == "__main__":
    top = int(sys.argv[sys.argv.index("--top") + 1]) if "--top" in sys.argv else 20
    now = msk_now()
    with init_db(settings.database_url)() as db:
        cands = run(db, now)["candidates"]
        bases, results = {}, []
        for c in cands:
            cid = main_category(db, c.metrics.product_id)
            if cid not in bases:
                bases[cid] = baseline(db, cid) if cid else None
            results.append((c, product_attention(db, c.metrics.product_id, bases[cid]), db.get(Category, cid).name if cid else "—"))
        obs_dist = observation_counts(db, cands)
        names = {cid: db.get(Category, cid).name for cid in bases if cid}
    results.sort(key=lambda r: (STATUS_ORDER[r[1].status], -(r[1].multiple or 0), CONF[r[1].confidence],
                                -(r[1].vpd_median or 0)))  # fmt: skip
    print("== НОРМА КАТЕГОРИЙ (нейтральные карточки)")
    for cid, b in bases.items():
        if b and b.n:
            print(f"  {names[cid]}: n={b.n} · медиана {num(b.median)}/д · p75 {num(b.p75)} · дней {b.days} · {b.confidence}"
                  + ("" if b.usable else " · НЕ пригодна"))
    print("\n== ЗАМЕРЫ ТОВАРОВ: кандидатов", len(cands), "· по числу объявлений с карточкой:", dict(sorted(obs_dist.items())))
    print("статусы:", dict(Counter(a.status for _, a, _ in results)))
    print(f"\n== TOP {top}")
    for i, (c, a, cat) in enumerate(results[:top], 1):
        att = f"{a.multiple:.1f}×" if a.multiple is not None else a.status
        print(f"{i:2}. {c.metrics.name} [{cat}] — {att} · vpd {num(a.vpd_median)} · карточек {a.obs_n} · {a.confidence} · 14д {c.metrics.listings_14d} · {rub(c.metrics.price_median)}")
    print("\n== 🔥 ЧТО РЕАЛЬНО СТОИТ ИССЛЕДОВАТЬ (подтверждено ≥ 2 объявлениями, ≥ 1.5× нормы)")
    strong = [r for r in results if r[1].status == "MEASURED" and r[1].multiple >= 1.5]
    for i, (c, a, cat) in enumerate(strong, 1):
        print(block(i, c, a, cat))
    print("\n== ⚡ НУЖНО ПОДТВЕРДИТЬ (одна сильная карточка)")
    for c, a, _ in results:
        if a.status == "ATTENTION_ANOMALY" or (a.status == "SINGLE" and a.multiple >= 1.5):
            print(f"  {c.metrics.name}: {a.multiple:.1f}× (vpd {num(a.vpd_median)}, одна карточка)")
    print("\n== ❌ ПОСЛЕ ПРОСМОТРОВ — НИЖЕ РЫНКА (< 0.8× нормы)")
    for c, a, _ in results:
        if a.multiple is not None and a.multiple < 0.8:
            print(f"  {c.metrics.name}: {a.multiple:.1f}× · vpd {num(a.vpd_median)} · карточек {a.obs_n} · 14д объявлений {c.metrics.listings_14d}")
    print("\n== 🕓 БЕЗ НОРМЫ КАТЕГОРИИ (просмотры есть, сравнить не с чем)")
    for c, a, cat in results:
        if a.status == "BASELINE_PENDING":
            print(f"  {c.metrics.name} [{cat}]: vpd {num(a.vpd_median)} · карточек {a.obs_n}")
    print("\n== TOP 10 подробно")
    for i, (c, a, cat) in enumerate(results[:10], 1):
        print(block(i, c, a, cat))
