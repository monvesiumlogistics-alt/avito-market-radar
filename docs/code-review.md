# Code Review — /report market crawl (08a809f..HEAD)

**Reviewed by**: reviewer agent (Python adaptation of skills/reviewer.md)
**Date**: 2026-10-03
**Files in diff**: 31 (app, scripts, tests); `pytest -q` = 121 passed, ruff clean
**Verdict**: CHANGES REQUESTED (no CRITICAL; one HIGH data-correctness bug)

## CRITICAL

None found.

## HIGH

- `app/services/market.py:502-529` — `pick_cards(group_cards(cards))` truncates every group to 2 cards (`per_group=2`), and the Find is then built from the truncated `group`: `prices`, `price_min/price_max` and `copies=len(group)` (line 529-545). "выставлено N раз" can never exceed 2 and the price range ignores the other copies, which defeats the grouping feature (AC on copies/price range).
  **Fix**: keep both lists: `for full in group_cards(cards)` -> `picked = pick_cards([full])[0]` for opening, and use `full` for `prices`/`copies` (e.g. make `pick_cards` return `(picked, full)` pairs, ordered by `picked[0]`). Add a test with 3+ copies asserting `copies == 3`.

## MEDIUM

- `app/services/market.py:553-574` — `_open_card` swallows every `Exception`, including the `_open_provider` failure raised from `_handover` (via `_before_load`, line 348) and `AttributeError: NoneType.fetch` when `self.provider is None` afterwards. A dead browser mid-subcategory just appends errors per card, then `_save(ok)` marks the subcategory crawled (`last_crawled_at` set, line 607) with no finds; the breaker (line 229) only sees `_collect` failures.
  **Fix**: in `_handover` let a reopen failure raise a dedicated `BrowserLost(Exception)` and re-raise it in the `except (StopRequested, ...)` tuples of `_open_card`/`_seller_date`/`crawl_subcategory`; or count card errors and return False (error status, no `last_crawled_at`) from `crawl_subcategory` if every opened card errored.
- `app/services/market.py:226-229,345` + `start()` line 152 — resume restores `run.loads`, so a run stopped at ~550/600 gets only ~50 loads after "Продолжаю". Status `budget` is not resumable, but `stopped/blocked` runs near the cap effectively die on resume with no hint.
  **Fix**: either document it in the reply ("загрузок уже 550/600") or reset the budget on resume (`self.loads = 0`, keep `run.loads` as a running total in a separate column).
- `app/services/market.py:336-337,316` — `_summary` re-sends all finds of the run, including those already sent by `_portion`; `sent` is ignored for the summary. Every find reaches Telegram twice, and a full summary can be 3-4 messages at 1 s delay each.
  **Fix**: if duplication is not an explicit requirement, summary lists only `sent == False` finds plus a count ("всего находок N"); otherwise note it in tech-design.
- `app/providers/avito_parser.py:35` — `"Вы робот"` added to `BLOCK_MARKERS`; `is_blocked` substring-matches the full HTML, so an item whose user-written description (or seller name) contains "Вы робот" / "Доступ ограничен" is treated as a captcha block: `ProviderBlocked`, up to 15 min wait for a human, run -> `blocked`. Item pages are new traffic for this check.
  **Fix**: for fetched item/profile pages test markers only in `<title>` and the first ~20 KB, or match `class="firewall-container` / title only; drop the bare "Вы робот".
- `app/providers/avito_browser.py:123-135` (`wait_unblocked`) — loop count `int(timeout_s // poll_s)` ignores time spent in `tab.content()`/`title()` (can hang up to Playwright default 30 s each, swallowed by `suppress`), so the real wait can far exceed `captcha_wait_minutes`, and the gate is held that whole time, starving the monitor. Also `/stop` is noticed only after a sleep tick.
  **Fix**: use `loop.time()` deadline: `while loop.time() < deadline`, and give `tab.content()` a `asyncio.wait_for(..., 10)`.
- `app/services/market_logic.py:166-170` — `split_message` hard-cut branch appends the over-long piece to `chunks` before flushing `current`, so message order is scrambled and `current` is glued to the tail. The only line that can realistically exceed 4096 is "Покрыто не полностью: ..." (18 subcats x ~12 sections), and a hard cut can split an `&amp;` entity (Telegram "can't parse entities").
  **Fix**: flush `current` first (`if current: chunks.append(current); current = ""`) before cutting; make the "Покрыто" line one line per subcategory or cut at `", "`.

## LOW

- `app/services/market.py:276-289` — `_lines` does 2 queries per find (min created_at, `db.get(Category)`): N+1. Fine for tens of finds; batch with one `GROUP BY group_key, category_id` query and a `{id: name}` dict if finds grow.
- `app/services/market.py:212-217` — `_finish` runs in `finally`; if it raises (SQLite busy) the exception escapes the task, `_summary` is skipped and the run stays `running` until next restart (`mark_interrupted`). Wrap in `try/except` + log.
- `app/services/market.py:261-264` — if `send_text` returns None (Telegram error) `_progress_id` stays None and a new progress message is attempted every minute; fine, but `db.get(...).progress_msg_id = None` write is pointless; also `CrawlRun.note` (db.py:84) and `BrowserGate.locked` (base.py:28-29) are never used: delete.
- `app/providers/avito_parser.py:127-143` — date strings are naive local time; `timezone_id="Europe/Moscow"` is set for the browser but `datetime.now()` is the PC clock. If the PC is not MSK ages shift by hours (age floor of 1 day hides most of it). Note in README or use `ZoneInfo("Europe/Moscow")` in clock.
- `app/providers/avito_parser.py:133-136` — "29 февраля" in a non-leap `now.year` returns None silently (ValueError branch); negligible.
- `app/services/notifier.py:59,66` — `disable_web_page_preview` is deprecated since aiogram 3.7 (requirements pins >=3.31); use `link_preview_options=LinkPreviewOptions(is_disabled=True)`.
- `app/services/market_logic.py:20-37,60-100` — thin wrappers (`calc_vpd`, `is_hot`, `date_checked`, `find_age`) are one-liners used once; acceptable for testability but `STOP_WORDS`/`MAX_ERRORS` constants live in logic while `MAX_ERROR_STREAK` is in market. Over-engineering is otherwise modest; `Opened` dataclass and `BrowserGate` are justified.
- `app/providers/base.py:66-72` — `fetch`/`wait_unblocked` are non-abstract with `NotImplementedError`/False default: fine, but make `fetch` abstract if `FakeProvider` in tests implements it.

## Verified OK (no finding)

- 7-day stop (`_collect`): promoted/undated cards skipped, page read to the end before stopping, `covered` computed from last in-limit card; `?s=104&pmin` plus `with_page` keeps query.
- Gate handover: `release()` then `acquire()` queues behind the waiting scanner (asyncio.Lock FIFO), `_owns_gate` flag prevents releasing a foreign lock; `_run` `finally` always closes provider, releases gate, writes status/loads (also on CancelledError -> `interrupted`).
- Tabs: `fetch` and `wait_unblocked` close their tab in `finally`; `__aexit__` is idempotent and `__aenter__` stops Playwright if launch fails.
- Budget/loads: counted in the single `_before_load`, +1 for warm-up and each handover reopen, persisted by `_save`/`_finish` on every exit.
- HTML: titles truncated before escaping (<=120 chars), category/error text escaped, each line self-contained for chunking.
- Monitor regression: `Scanner` behaves as before without gate; only new behavior is HTTP 403/429/439 raising `ProviderBlocked` (intended, ADR-004).

## Good

- Pure logic split from I/O (`market_logic.py`), heavily unit-tested; fixtures from live pages; 121 tests green.
- Resume/interrupt model is simple (`last_run_id == run.id`), commit-per-subcategory keeps state consistent.
- Captcha handled by a human (ADR-005), not bypassed; breaker + fetch timeout + budget bound the blast radius.

## Coverage gaps

- No test for groups with more than 2 copies (the HIGH bug would have been caught).
- No test for reopen failure inside `_handover` and for `split_message` with an over-long line after a pending `current`.
