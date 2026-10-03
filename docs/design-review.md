# Design Review — «Что выложить» (проверка рынка по кнопке)

**Reviewed by**: design-reviewer agent (claude-workflow Phase 3, reviewer.md Design Review Mode, adapted to Python/Playwright/SQLAlchemy/aiogram)
**Date**: 2026-10-03
**Inputs**: `docs/tech-design.md`, `docs/requirements.md` (rev.3), `docs/decisions.md` (ADR-001..003), code in `app/` and `tests/` (23 tests), reference `avito_niche/catalog.js` (read only)
**Verdict**: **READY FOR PLANNING** (0 BLOCKER, 4 MAJOR, 11 MINOR). The 4 MAJOR fixes are small. Carry them into the plan as required items, or patch the design in place before planning.

Severity mapping to phases/03: BLOCKER = CRITICAL/HIGH (blocks planning). MAJOR = MEDIUM (must be fixed in the design or the plan, does not block). MINOR = LOW (the planner decides).

---

## BLOCKER

None.

---

## MAJOR

### M1. Browser handover to the D&G monitor is unsafe as sketched (§5 loop, «Соседство с мониторингом»)
The design opens with `async with gate.hold(), provider_factory() as p:`. Inside that block it then does «закрыть браузер, отдать gate, взять снова, открыть браузер». Against the current code this causes four problems:
- If the code releases the lock inside `async with gate.hold()` and an exception (block, cancel, launch failure) fires before it re-acquires, the outer `__aexit__` releases an unlocked `asyncio.Lock`. That raises `RuntimeError`, which hides the real error and leaves the run status unset.
- Closing `p` inside the block and letting the outer `async with` exit it again calls `ctx.close()` and `pw.stop()` twice. `AvitoBrowserProvider.__aexit__` (`app/providers/avito_browser.py:28-32`) does not reset `_ctx`/`_pw`.
- If the same instance is re-entered, `_warmed` stays `True` (`avito_browser.py:39-42`), so the promised warm-up after reopen never happens. A cold goto to a search page is exactly what the existing code avoids.
- The yield point exists only between subcategories. The AC-5.3 bound of 15 min therefore holds only on the happy path. A subcategory is up to about 30 loads (5 pages + 12 cards + up to 12 profiles + warm-up). With the 45 s goto timeout plus a 15 s `wait_for_selector`, it can exceed 15 min on a slow or flaky network.

**Fix (one structure, less code than the sketch)**: do not nest `async with` around the whole loop. Use a single per-load hook in `MarketCrawler`, which every `fetch` goes through:
```
async def _before_load(self):
    if self.stop_requested: raise StopRequested
    if self.run.loads >= budget: raise BudgetExhausted
    if self.gate.contended:                     # yield at load granularity -> delay <= 1 load
        await self.p.__aexit__(None, None, None)
        self.gate.release(); await self.gate.acquire()
        self.p = self.provider_factory(); await self.p.__aenter__()   # new instance -> fresh warm-up
```
The top level is `await gate.acquire(); try: ... finally: close p if open; gate.release() if gate.locked() and we own it`. `crawl_subcategory` keeps its state in memory, so yielding mid-subcategory loses nothing. The «перед каждой подкатегорией» check goes away. `test_scanner.py` should assert that the scan gets the gate within 1 load, not within 1 subcategory.

### M2. The budget counter lives on the provider and resets on every reopen or resume (§3 avito_browser «Счётчик loads», §5 «бюджет считается от уже сделанных loads»)
The provider is recreated on each yield (M1) and on each resume, so a provider-level `loads` restarts at 0. NFR-1 and AC-4.5 would then be silently overrun, by up to 600 per handover. Loads made in a partially crawled subcategory are also never written (step 6 writes `run.loads` only at the end of the subcategory).
**Fix**: increment `run.loads` in `_before_load` (M1) and count warm-up through the same hook (or add +1 on reopen). Persist it together with the subcategory commit (see m8). The provider needs no counter.

### M3. AC-2.4b fields are missing from the data model (§4 `finds`)
AC-2.4b (MUST) requires the total view count and the page date to be saved. `finds` stores `vpd`, `today` and `age_days`, but not `views` or the date from the card page. vpd cannot be recomputed or audited later, for example after a profile date replaces the page date.
**Fix**: add `views int` and `page_date datetime NULL` to `finds`, and `seller_date datetime NULL` if the profile check found one. State explicitly that only finds are persisted, not every opened card. That is the reading this review accepts as sufficient for AC-2.4b in a hobby tool.

### M4. No circuit breaker for systemic failure (§5 `except Exception: cat.last_status=error`)
A realistic failure: Chromium crashes, a `Target closed` error occurs, or the profile window is closed by hand. After that, every following `crawl_subcategory` fails instantly with a non-block exception. The loop then walks the whole remaining order, which can be about 180 subcategories. Each one is marked as passed in this run (`last_run_id`), so resume skips them. The summary carries about 180 «ошибка» lines, which means a burst of Telegram messages. That defeats the AC-5.1 and AC-4.4 intent of «состояние сохранено для продолжения».
**Fix**: after 3 consecutive subcategory errors, stop with `status=failed` (this status is resumable per §5) and send «⚠️ Проверка упала, найденное сохранено». On a subcategory error, set `last_run_id` but **not** `last_crawled_at`, so the next run retries it by rotation. The summary should list at most 10 errors plus «и ещё N».

---

## MINOR

- **m1. §5 crawl_subcategory step 1 (stop rule)**: finish the current page after the first old non-promoted card; do not cut it mid-page. Avito injects out-of-order blocks («из соседних регионов», recommendations) that can carry an old date early in the page. The fix is the same rule and one page of slack. Also parse «месяц назад», «N месяцев назад» and «25 сентября 2025» (with a year) as old. Otherwise an unparsed date never stops paging and quietly burns up to 5 pages per subcategory.
- **m2. §6 `pick_cards`**: if it returns 12 pre-reserved slots (2 per group) while the second card opens only conditionally, the 12-card cap ends up underused (6 groups examined instead of up to 12). Specify it as ordered groups plus a running counter of opened pages, capped at `REPORT_CARDS_PER_SUBCAT`.
- **m3. §5 step 4**: the source of the age in `vpd = views / max(age, 1)` is not stated. It should be the card-page date, with the search-page date only as a fallback.
- **m4. §5 step 5 / AC-3.4**: with `CHECK_SELLER_DATE=false`, which is the expected outcome if the live check shows raising does not change the card date, every find would be labelled «дата не проверена». In that case the card date is the real date: set `date_checked=true` (or use a neutral label).
- **m5. §6 `crawl_order`**: grouping by section forces the whole section to be crawled, including subcategories crawled yesterday that had 0 finds. Rotation then works per section, not per subcategory as AC-4.6 asks. This is acceptable for a hobby tool (the catalog is still covered in about 5 runs, and the budget-skipped tail still comes first). Either accept it explicitly, or order globally and flush a portion whenever the section changes.
- **m6. §3 notifier / §7 Telegram**:
  - `send_text` does not pass `disable_web_page_preview`. Portions full of `<a href>` would each expand a link preview. Add the flag.
  - Skip `edit_text` when the text is unchanged, because Telegram raises «message is not modified».
  - Cap the title per line (about 120 characters) so that one line can never exceed 4096.
  - Sleep about 1 s between `split_message` chunks. aiogram does not retry `RetryAfter`, and the notifier swallows the error, so the chunk would be lost.
  - On resume, send a new progress message instead of editing the one from a previous session.
- **m7. §3 `fetch` block detection**: `is_blocked` looks at HTML and title only. Also treat a `goto` response status of 403 or 429 as `ProviderBlocked` (AC-5.1); it costs one line.
- **m8. §5 step 6**: write the finds, the category update and `run.loads` in **one** commit per subcategory. Otherwise a crash between the writes makes resume redo the subcategory and insert duplicate `finds` rows into the same run. Results from a subcategory interrupted by `/stop` are discarded and redone on resume. That is acceptable, but state it.
- **m9. §5 watchdog**: Playwright calls without a timeout (`tab.content()`, `new_page()`) can hang while the crawler holds the gate. The monitor then starves, and APScheduler `max_instances=1` skips every later scan (AC-5.4 in spirit). Wrap each `fetch` in `asyncio.timeout(120)` (py3.11). A timeout counts as a card or subcategory error.
- **m10. Persistent profile reopen**: on Windows, launching Chromium on the same `user_data_dir` right after `ctx.close()` can occasionally fail with «profile in use». One retry after 2 s in the crawler's reopen path is enough. The scanner already reports a launch error gracefully (`scanner.py:96-98`).
- **m11. Doc hygiene**: in §5, «чуть больше оценки NFR-2 (40–50)» is stale, because NFR-2 is already 55–65 per ADR-003. All of §11 «Решения, которые стоит подтвердить» is already accepted in ADR-003; mark it resolved. `REPORT_SECTIONS` should default in code to the 25 `TOP` slugs, with `.env` only overriding them, so `.env.example` does not carry a 400-character line.

---

## Traceability (MUST ACs → design)

| AC | Design | Status |
|---|---|---|
| 1.1 / 1.2 / 1.3 | §5 Старт, §7, §1 (no scheduler job) | OK |
| 2.1 | §4 categories + 30-day refresh, `parse_subcategories` (catalog.js regex) | OK |
| 2.2 / 2.3 | §5 step 1, `last_days_covered`, §7 summary | OK (m1) |
| 2.4 / 2.4a / 2.4c | §5 steps 2–3, §6 `group_cards`/`pick_cards` | OK (m2) |
| 2.4b | §4 finds | **M3** |
| 2.5 / 2.6 | §5 step 4, §6 `sort_finds`, §7 format | OK (m3) |
| 3.1–3.5 | §5 steps 4–5 | OK (m4); live feasibility to be verified in iteration 1 per ADR-003 |
| 4.1 / 4.2 | §5 loop, §7 | OK (m6) |
| 4.3 | StopRequested before each load | OK |
| 4.4 | `last_run_id == run.id`, startup `running→interrupted`, 12 h by `started_at` | OK (M2, m8) |
| 4.5 / 4.6 | `crawl_order`, status `budget` | OK (m5) |
| 5.1 | ProviderBlocked → alert, summary, `status=blocked` | OK (m7) |
| 5.2 | per-subcategory/card try | OK (**M4**) |
| 5.3 | BrowserGate | **M1** |
| 5.4 | scanner change limited to `gate` param, default `None` keeps 23 tests | OK (m9) |
| 6.1 / 6.2 | ReplyKeyboard + existing chat filter | OK |
| 8.1 | `finds.china_price` | OK |

The NFRs are covered: NFR-1 (M2), NFR-3/5/6/7 OK.

Design Review Mode checklist, adapted: there are no HTTP endpoints, and authorization is the existing `F.chat.id` filter (OK). The data model has timestamps and the needed indexes (`categories.url` UNIQUE, `finds.group_key`). An index on `finds.run_id` is not needed at these volumes. No cache. The edge-case table in §9 has 6 feature-specific items. Non-obvious decisions are recorded in ADR-002/003.

---

## Good

- The lock is used correctly for this codebase. Python ≥3.11 `asyncio.Lock` does not let the releaser barge back in while a waiter exists, so the gate is fair to the monitor. Scanner, crawler and handlers all run on one event loop with synchronous SQLite sessions, so DB writes do not run concurrently. No deadlock path was found: notifier and handlers never touch the gate, and `/check` waits on it from its own aiogram task while `/stop` stays responsive.
- The persistent-profile single-instance constraint is handled by strict serialization instead of sharing a context. That is the right call given that `launch_persistent_context` locks `user_data_dir`.
- The change to the existing monitor is minimal and optional (`gate=None`). `search()` becomes a thin wrapper over `fetch()`, and `FakeProvider` in the tests keeps working.
- Pure logic is split out into `market_logic.py` and tested on fixtures (NFR-7). `split_message` cuts only on line boundaries, and every line has self-contained HTML tags.
- Resume via `last_run_id == run.id` instead of a stored queue: no extra table, and rotation does the rest.
- `pmin` in the URL plus a local price re-check is cheap and correct.
- YAGNI is respected: no Alembic, no repositories, no separate provider, no new dependencies, and only 2 new files. Nothing in the design is over-engineered for a single-user tool. The 7 run statuses are the only borderline item, and each one maps to a distinct message or resume rule.
