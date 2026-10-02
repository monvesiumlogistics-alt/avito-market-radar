import asyncio
import logging
import time
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from app.db import ListingRow, WatchRule
from app.models import Listing, SearchUrl
from app.providers.base import AvitoProvider, ProviderBlocked
from app.services.matcher import matches

log = logging.getLogger(__name__)


class Notifier(Protocol):
    async def send_listing(self, row: ListingRow, rule_name: str) -> bool: ...
    async def send_text(self, text: str) -> None: ...


class Scanner:
    """load rules -> provider.search -> match -> dedupe/save -> notify."""

    def __init__(
        self,
        session_factory: sessionmaker,
        provider_factory: Callable[[], AvitoProvider],
        notifier: Notifier,
        *,
        interval_minutes: int = 60,
        initial_scan_notify: bool = False,
        max_pages: int = 3,
        retry_delays: tuple[float, ...] = (5, 20),
        send_delay: float = 1.0,
    ):
        self.session_factory = session_factory
        self.provider_factory = provider_factory
        self.notifier = notifier
        self.interval_minutes = interval_minutes
        self.initial_scan_notify = initial_scan_notify
        self.max_pages = max_pages
        self.retry_delays = retry_delays
        self.send_delay = send_delay
        self.paused = False  # ponytail: пауза в памяти, после рестарта мониторинг снова идёт
        self.blocked = False
        self.last_run_at: datetime | None = None
        self._lock = asyncio.Lock()

    @property
    def busy(self) -> bool:
        return self._lock.locked()

    async def run_watch_rules(self, force: bool = False) -> list[str]:
        """Возвращает короткий отчёт по правилам (для /check)."""
        if self.paused and not force:
            log.info("[SCAN] на паузе, пропуск")
            return ["на паузе"]
        if self._lock.locked():
            return ["проверка уже идёт"]
        async with self._lock:
            now = datetime.now()
            with self.session_factory() as db:
                rules = [
                    r
                    for r in db.scalars(select(WatchRule).where(WatchRule.enabled)).all()
                    if force or self._due(r, now)
                ]
            if not rules:
                return ["нет правил к проверке"]
            report = []
            try:
                async with self.provider_factory() as provider:
                    for rule in rules:
                        try:
                            report.append(await self._scan_rule(provider, rule))
                        except ProviderBlocked:
                            raise
                        except Exception as e:
                            log.exception("[SCAN] правило %r упало", rule.name)
                            report.append(f"{rule.name}: ошибка {type(e).__name__}")
            except ProviderBlocked as e:
                log.error("[BLOCKED] %s", e)
                if not self.blocked:
                    await self.notifier.send_text(
                        f"⚠️ Avito ограничил доступ: «{e}».\n"
                        "Мониторинг продолжит попытки по расписанию. Если не пройдёт: останови бота, "
                        "запусти python -m app.auth, пройди проверку/войди вручную и запусти снова."
                    )
                self.blocked = True
                report.append(f"⚠️ Avito: {e}")
            except Exception as e:  # например, профиль браузера занят запущенным app.auth
                log.exception("[SCAN] браузер не запустился")
                report.append(f"ошибка браузера: {type(e).__name__}: {e}")
            else:
                if self.blocked:
                    await self.notifier.send_text("✅ Доступ к Avito восстановлен")
                self.blocked = False
            self.last_run_at = datetime.now()
            return report

    def _due(self, rule: WatchRule, now: datetime) -> bool:
        if rule.last_checked_at is None:
            return True
        interval = rule.check_interval_minutes or self.interval_minutes
        return now - rule.last_checked_at >= timedelta(minutes=interval) - timedelta(seconds=30)

    async def _fetch(self, provider: AvitoProvider, search: SearchUrl, page: int) -> list[Listing]:
        for attempt, delay in enumerate((*self.retry_delays, None), start=1):
            try:
                return await provider.search(search, page)
            except ProviderBlocked:
                raise
            except Exception as e:
                if delay is None:
                    raise
                log.warning("[FETCH] попытка %d не удалась (%s), повтор через %ss", attempt, e, delay)
                await asyncio.sleep(delay)
        raise AssertionError("unreachable")

    def _known_ids(self, rule_id: int, ids: list[str]) -> set[str]:
        with self.session_factory() as db:
            rows = db.scalars(
                select(ListingRow.external_id).where(ListingRow.rule_id == rule_id, ListingRow.external_id.in_(ids))
            )
            return set(rows)

    async def _scan_rule(self, provider: AvitoProvider, rule: WatchRule) -> str:
        started = time.monotonic()
        initial = rule.last_checked_at is None
        log.info("[SCAN] %s%s", rule.name, " (первый скан: индексация)" if initial else "")

        fetched: dict[str, Listing] = {}
        for raw in rule.search_urls:
            search = SearchUrl(raw.get("label", ""), raw["url"])
            for page in range(1, self.max_pages + 1):
                items = await self._fetch(provider, search, page)
                for item in items:
                    fetched.setdefault(item.external_id, item)
                # вся страница новая -> за интервал могло выйти больше, листаем дальше
                if initial or not items or self._known_ids(rule.id, [i.external_id for i in items]):
                    break
        log.info("[FETCH] %d listings", len(fetched))

        now = datetime.now()
        notify_new = not initial or self.initial_scan_notify
        new_matched = 0
        with self.session_factory() as db:
            known = {
                row.external_id: row
                for row in db.scalars(
                    select(ListingRow).where(
                        ListingRow.rule_id == rule.id, ListingRow.external_id.in_(list(fetched))
                    )
                )
            }
            for ext_id, item in fetched.items():
                if ext_id in known:
                    known[ext_id].last_seen_at = now
                    continue
                ok = matches(item, rule)
                new_matched += ok
                db.add(
                    ListingRow(
                        rule_id=rule.id,
                        **item.model_dump(exclude={"parsed_at"}),
                        first_seen_at=now,
                        last_seen_at=now,
                        matched=ok,
                        notify_status=("pending" if notify_new else "skipped") if ok else None,
                    )
                )
            db.get(WatchRule, rule.id).last_checked_at = now
            db.commit()
            pending = db.scalars(
                select(ListingRow)
                .where(ListingRow.rule_id == rule.id, ListingRow.notify_status == "pending")
                .order_by(ListingRow.first_seen_at, ListingRow.id)
            ).all()
        log.info("[MATCH] %d new matching listings", new_matched)

        sent = 0
        for row in pending:
            log.info("[NEW] avito_id=%s %s", row.external_id, row.title[:60])
            if await self.notifier.send_listing(row, rule.name):
                sent += 1
                with self.session_factory() as db:
                    stored = db.get(ListingRow, row.id)
                    stored.notify_status, stored.notified_at = "sent", datetime.now()
                    db.commit()
            await asyncio.sleep(self.send_delay)  # лимиты Telegram на частоту сообщений

        log.info("[SCAN] finished in %.1fs", time.monotonic() - started)
        if initial and not self.initial_scan_notify:
            return f"{rule.name}: проиндексировано {len(fetched)}, подходящих {new_matched} (без уведомлений)"
        return f"{rule.name}: получено {len(fetched)}, новых подходящих {new_matched}, отправлено {sent}"
