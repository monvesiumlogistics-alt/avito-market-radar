from types import SimpleNamespace

from app.main import build_scheduler


async def test_scheduler_only_scan():
    async def run_watch_rules(force=False): ...

    scheduler = build_scheduler(SimpleNamespace(run_watch_rules=run_watch_rules, interval_minutes=60))
    assert {j.id for j in scheduler.get_jobs()} == {"scan"}  # AC-1.3: проверки рынка по расписанию нет


async def test_scheduler_daily_sweep_and_catchup():
    async def run_watch_rules(force=False): ...

    crawler = SimpleNamespace(daily=lambda: False)
    scanner = SimpleNamespace(run_watch_rules=run_watch_rules, interval_minutes=60)
    jobs = {j.id: j for j in build_scheduler(scanner, crawler, "09:00").get_jobs()}
    assert set(jobs) == {"scan", "sweep", "sweep_catchup"}
    assert str(jobs["sweep"].trigger) == "cron[hour='9', minute='0']"
    assert {j.id for j in build_scheduler(scanner, crawler, "").get_jobs()} == {"scan"}  # расписание выключено
