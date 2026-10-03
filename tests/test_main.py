from types import SimpleNamespace

from app.main import build_scheduler


async def test_scheduler_only_scan():
    async def run_watch_rules(force=False): ...

    scheduler = build_scheduler(SimpleNamespace(run_watch_rules=run_watch_rules, interval_minutes=60))
    assert {j.id for j in scheduler.get_jobs()} == {"scan"}  # AC-1.3: проверки рынка по расписанию нет
