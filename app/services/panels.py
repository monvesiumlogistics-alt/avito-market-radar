"""Сообщения бота в стиле панели прогресса: заголовок, подписи обычным текстом, числа в <code>, иконки
(premium_emoji -> UnigramIcons, иначе обычные эмодзи). Только текст, без I/O."""

import html
from datetime import datetime

from app.services.market_logic import Margin, icon, short_title
from app.services.market_logic import _rub as rub


def start_text(premium: bool = False) -> str:
    i = lambda e: icon(e, premium)  # noqa: E731
    return "\n".join([
        f"<b>{i('⚙')} AvitoHunter</b>",
        "Мониторинг Avito и поиск, что выложить под заказ.",
        "",
        f"<b>{i('🔎')} Что выложить</b> (проверка рынка, час-полтора)",
        "/report — запустить или продолжить · /stop — остановить",
        "/sweep — обход всех категорий без карточек (сам каждый день в 09:00)",
        "/radar — MARKET RADAR: что изменилось на рынке",
        "/top [дней] — лучшие находки из базы",
        "/export — все находки файлом CSV",
        "/price номер юани — маржа находки",
        "/cats — разделы · /skip текст · /unskip текст — выключить или вернуть",
        "",
        f"<b>{i('🔔')} Мониторинг</b>",
        "/status · /watchlist · /check · /last · /pause · /resume",
    ])  # fmt: skip


def status_header(blocked: bool, premium: bool = False) -> str:
    if blocked:
        return f"<b>{icon('⚠️', premium)} Avito ограничил доступ</b>"
    return f"<b>{icon('✅', premium)} AvitoHunter работает</b>"


def status_rule(
    name: str, state: str, last_checked: str, found: int, new_today: int, no_urls: bool = False, premium: bool = False
) -> list[str]:
    i = lambda e: icon(e, premium)  # noqa: E731
    lines = [
        f"<b>{html.escape(name)}</b>",
        f"{i('ℹ️')} Статус: <code>{state}</code>",
        f"{i('🔄')} Последняя проверка: <code>{last_checked}</code>",
        f"{i('✅')} Найдено объявлений: <code>{found}</code>",
        f"{i('🔔')} Новых сегодня: <code>{new_today}</code>",
    ]
    if no_urls:
        lines.append(f"{i('❗️')} Нет AVITO_SEARCH_URLS в .env")
    return lines + [""]


def status_next(next_run: datetime, premium: bool = False) -> str:
    return f"{icon('⏰', premium)} Следующая проверка: <code>~{next_run:%H:%M}</code>"


def startup_text(rule_name: str, every: int, enabled: bool, premium: bool = False) -> str:
    i = lambda e: icon(e, premium)  # noqa: E731
    lines = [f"<b>{i('✅')} AvitoHunter запущен</b>", f"{i('🔎')} Жми «🔎 Проверить рынок» или /report"]
    if enabled:
        lines.append(f"{i('🔄')} Мониторинг «{html.escape(rule_name)}»: каждые <code>{every}</code> мин")
    return "\n".join(lines)


def captcha_alert(minutes: int, premium: bool = False) -> str:
    return (
        "🧩 <b>Avito просит проверку</b>\n"
        f"{icon('⏰', premium)} Пройди её в окне браузера бота. Жду до <code>{minutes}</code> мин"
    )


def captcha_passed(premium: bool = False) -> str:
    return f"{icon('✅', premium)} Проверка пройдена, продолжаю"


def block_alert(reason: object, premium: bool = False) -> str:
    return "\n".join([
        f"<b>{icon('⚠️', premium)} Avito ограничил доступ</b>",
        f"{icon('ℹ️', premium)} Причина: <code>{html.escape(str(reason))}</code>",
        f"{icon('🔄', premium)} Мониторинг продолжит попытки по расписанию.",
        "Если не пройдёт: останови бота, запусти <code>python -m app.auth</code>, пройди проверку "
        "или войди вручную и запусти снова.",
    ])  # fmt: skip


def block_restored(premium: bool = False) -> str:
    return f"{icon('✅', premium)} Доступ к Avito восстановлен"


def price_panel(
    find_id: int, title: str, price_rub: int, m: Margin, rate: float, ground_per_kg: float, premium: bool = False
) -> str:
    """/price: закупка, доставка, себестоимость, продажа, маржа наземкой и авиа."""
    i = lambda e: icon(e, premium)  # noqa: E731
    return "\n".join([
        f"<b>{i('💵')} Находка #{find_id}</b>",
        short_title(title, 60),
        "",
        f"{i('💵')} Закупка: <code>¥{m.yuan} × {rate:g}</code>",
        f"{i('⏩')} Доставка: <code>{m.kg:g} кг</code> × <code>{ground_per_kg:g} ₽</code>",
        f"{i('ℹ️')} Себестоимость: <code>{rub(m.cost)} ₽</code>",
        f"{i('📈')} Продажа: <code>{rub(price_rub)} ₽</code>",
        f"{i('🏆')} Маржа наземкой: <code>{rub(m.margin)} ₽ ({m.pct}%)</code>",
        f"{i('⏩')} Маржа авиа: <code>{rub(m.air_margin)} ₽ ({m.air_pct}%)</code>",
    ])  # fmt: skip
