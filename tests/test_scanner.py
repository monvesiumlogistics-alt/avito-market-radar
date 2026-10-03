import asyncio
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select

from app.config import Settings
from app.db import ListingRow, init_db, sync_default_rule
from app.models import Listing, SearchUrl
from app.providers.base import AvitoProvider, BrowserGate, ProviderBlocked
from app.services.scanner import Scanner


def make(ext_id: str, title: str = "Dolce Gabbana куртка", price: int = 1500) -> Listing:
    return Listing(external_id=ext_id, title=title, price=price, url=f"https://www.avito.ru/moskva/odezhda/x_{ext_id}")


class FakeProvider(AvitoProvider):
    def __init__(self):
        self.pages: dict[int, list[Listing]] = {1: []}
        self.calls: list[int] = []
        self.blocked = False

    async def search(self, search: SearchUrl, page: int = 1) -> list[Listing]:
        self.calls.append(page)
        if self.blocked:
            raise ProviderBlocked("Доступ ограничен")
        return self.pages.get(page, [])


class FakeNotifier:
    def __init__(self):
        self.sent: list[str] = []
        self.texts: list[str] = []
        self.fail = False

    async def send_listing(self, row, rule_name):
        if self.fail:
            return False
        self.sent.append(row.external_id)
        return True

    async def send_text(self, text):
        self.texts.append(text)


def setup(tmp_path, **kw):
    session_factory = init_db(f"sqlite:///{tmp_path}/test.db")
    sync_default_rule(session_factory, Settings(_env_file=None, avito_search_urls="Все|https://www.avito.ru/all?q=x"))
    provider, notifier = FakeProvider(), FakeNotifier()
    scanner = Scanner(session_factory, lambda: provider, notifier, retry_delays=(), send_delay=0, **kw)
    return SimpleNamespace(db=session_factory, provider=provider, notifier=notifier, scanner=scanner)


async def test_first_scan_indexes_without_notifications(tmp_path):
    t = setup(tmp_path)
    t.provider.pages[1] = [make("1"), make("2")]
    await t.scanner.run_watch_rules(force=True)
    assert t.notifier.sent == []
    with t.db() as db:
        assert db.scalar(select(func.count()).select_from(ListingRow)) == 2


async def test_initial_scan_notify_true_sends_existing(tmp_path):
    t = setup(tmp_path, initial_scan_notify=True)
    t.provider.pages[1] = [make("1")]
    await t.scanner.run_watch_rules(force=True)
    assert t.notifier.sent == ["1"]


async def test_new_listing_sent_once_and_survives_restart(tmp_path):
    t = setup(tmp_path)
    t.provider.pages[1] = [make("1")]
    await t.scanner.run_watch_rules(force=True)  # индексация

    t.provider.pages[1] = [make("2"), make("1")]
    await t.scanner.run_watch_rules(force=True)
    assert t.notifier.sent == ["2"]

    await t.scanner.run_watch_rules(force=True)  # та же выдача
    assert t.notifier.sent == ["2"]

    # "перезапуск": новый сканер поверх той же БД
    restarted = Scanner(t.db, lambda: t.provider, t.notifier, retry_delays=(), send_delay=0)
    await restarted.run_watch_rules(force=True)
    assert t.notifier.sent == ["2"]


async def test_non_matching_and_replica_not_sent(tmp_path):
    t = setup(tmp_path)
    t.provider.pages[1] = [make("1")]
    await t.scanner.run_watch_rules(force=True)
    t.provider.pages[1] = [
        make("2", "Dolce Gabbana реплика"),
        make("3", "Officine Creative туфли"),
        make("4", price=5000),
        make("5"),
        make("1"),
    ]
    await t.scanner.run_watch_rules(force=True)
    assert t.notifier.sent == ["5"]


async def test_failed_send_retried_next_scan(tmp_path):
    t = setup(tmp_path)
    t.provider.pages[1] = [make("1")]
    await t.scanner.run_watch_rules(force=True)
    t.provider.pages[1] = [make("2"), make("1")]
    t.notifier.fail = True
    await t.scanner.run_watch_rules(force=True)
    t.notifier.fail = False
    await t.scanner.run_watch_rules(force=True)
    assert t.notifier.sent == ["2"]


async def test_fully_new_page_fetches_next_page(tmp_path):
    t = setup(tmp_path)
    t.provider.pages[1] = [make("1")]
    await t.scanner.run_watch_rules(force=True)
    t.provider.calls.clear()
    t.provider.pages = {1: [make("4"), make("3")], 2: [make("2"), make("1")]}
    await t.scanner.run_watch_rules(force=True)
    assert t.provider.calls == [1, 2]
    assert sorted(t.notifier.sent) == ["2", "3", "4"]


async def test_block_alerts_once_and_recovers(tmp_path):
    t = setup(tmp_path)
    t.provider.blocked = True
    await t.scanner.run_watch_rules(force=True)
    await t.scanner.run_watch_rules(force=True)
    assert len(t.notifier.texts) == 1 and "ограничил" in t.notifier.texts[0]
    t.provider.blocked = False
    await t.scanner.run_watch_rules(force=True)
    assert "восстановлен" in t.notifier.texts[-1]


async def test_paused_skips_scheduled_but_not_forced(tmp_path):
    t = setup(tmp_path)
    t.scanner.paused = True
    assert await t.scanner.run_watch_rules() == ["на паузе"]
    assert t.provider.calls == []
    await t.scanner.run_watch_rules(force=True)
    assert t.provider.calls == [1]


async def test_scan_waits_gate(tmp_path):
    gate = BrowserGate()
    t = setup(tmp_path, gate=gate)
    t.provider.pages[1] = [make("1")]
    await gate.acquire()  # проверка рынка держит браузер
    task = asyncio.create_task(t.scanner.run_watch_rules(force=True))
    await asyncio.sleep(0.05)
    assert not task.done() and t.provider.calls == [] and gate.contended
    gate.release()
    await task
    assert t.provider.calls == [1] and not gate.locked and not gate.contended


async def test_gate_released_after_block_and_error(tmp_path):
    gate = BrowserGate()
    t = setup(tmp_path, gate=gate)
    t.provider.blocked = True
    await t.scanner.run_watch_rules(force=True)
    assert not gate.locked


async def test_gate_contended_only_while_waiter_exists():
    gate = BrowserGate()
    assert not gate.contended
    await gate.acquire()
    assert not gate.contended  # владелец — не ждущий
    waiter = asyncio.create_task(gate.acquire())
    await asyncio.sleep(0)
    assert gate.contended
    gate.release()
    await waiter
    assert not gate.contended and gate.locked
    gate.release()


async def test_provider_fetch_default_not_implemented():
    with pytest.raises(NotImplementedError):
        await FakeProvider().fetch("https://x")
