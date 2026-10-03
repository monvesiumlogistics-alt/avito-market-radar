# Plan Review: «Что выложить» (проверка рынка по кнопке)

**Reviewed by**: plan-reviewer agent (claude-workflow Phase 5, adapted NestJS → Python/pytest/ruff)
**Date**: 2026-10-03
**Design doc**: docs/tech-design.md · **Design review**: docs/design-review.md · **Requirements**: docs/requirements.md rev.3 · **Decisions**: ADR-001..003
**Plan doc**: docs/implementation-plan.md (13 iterations)
**Code checked**: `app/` (base.py, avito_browser.py, avito_parser.py, scanner.py, notifier.py, handlers.py, main.py, config.py, db.py), `tests/` (23 tests: config 1, matcher 6, parser 8, scanner 8)

---

## Verdict: NEEDS_REVISION

0 BLOCKER, 3 MAJOR, 12 MINOR. All three MAJOR fixes are edits to the plan text, with no redesign. The design-review carry-over is complete: M1–M4 and m1–m11 all map to an iteration (table below).

---

## BLOCKER Issues

None. Ordering is sound: config/DB (I2) → parser (I3) → provider fetch (I7) → crawler (I9) all come before their users, and there are no cycles. The first iteration has no external blockers apart from the orchestrator stopping the bot, which the plan states.

---

## MAJOR Issues (must fix before starting)

### MJ1: Parallel waves share one working tree, the whole-suite gate and the live bot
**Iterations**: Wave 1 {1,2,4,6,8}, Wave 2 {3,5,7}; I1 and I7 trigger restarts of the live bot.
**Problem**: The file sets really are disjoint. Two things still break when implementers work in parallel in the same folder:
- Every gate runs the **whole** suite (`pytest` + `ruff check .`). If I6 is halfway through `test_scanner.py`, or I4 has half-written `test_market_logic.py`, I2's gate fails. The red result belongs to someone else, so the gates are non-deterministic.
- The D&G bot runs from this same tree (`python -m app.main`; two `python.exe` processes are running now). I1's done criterion is "bot restarted by orchestrator", and I7 asks for a manual restart. If the restart happens while I6 (`scanner.py`, `base.py`) or I3 (`avito_parser.py`, which the monitor uses) is mid-edit, the user's live monitor starts on half-written code. That breaks the AC-5.4/NFR-4 intent.
- `docs/` and the plan's future work are uncommitted (`git status`: `?? docs/`, `M NEXT.md`), so there is no clean baseline to fall back to.
**Fix**: Add an "Execution rules" block to the plan:
1. Commit the baseline (docs + NEXT.md) before I1.
2. Each parallel implementer works in its own git worktree (`isolation: "worktree"`). The orchestrator merges at the wave boundary and runs the full gate once on the merged tree. If worktrees are not used, the waves run sequentially.
3. The live bot is (re)started only from a green, committed wave boundary, never mid-wave. I1's restart therefore uses the last green commit (it can happen right away, because I1 changes no `app/` code). I7's manual check happens after Wave 2 is merged.

### MJ2: Several MUST ACs have no test in any done criterion
**Problem**: The plan says that tests live in the iteration that adds the code, but the done checkboxes do not cover these MUST ACs. Most of them appear only as "Work" text, so an implementer can tick every box without them:

| AC | Where the work is | Missing test / checkbox |
|---|---|---|
| AC-2.1 subcategories from the section page, stored, refreshed after 30 days, `REPORT_MAX_SUBCATS` | I10 "discover sections" | no done item at all |
| AC-2.2 page limit `REPORT_MAX_PAGES` | I9 | only promo and old-card stops are tested |
| AC-2.3 "покрыто N дней из 7" | I9 `days_covered`, I5 summary | not tested in either |
| AC-2.4 price cut (search level, local re-check even with `pmin`) | I9 | only the age cut is tested |
| AC-2.4b views / `page_date` / `seller_date` persisted on the find | I2 columns, I9 write | only the column existence is tested |
| AC-2.6 line content (price range, best vpd, «выставлено N раз», «дата не проверена») | I5 `format_find` | only escape and split are tested |
| AC-3.2 / AC-3.3 profile date >7 → drop; found → recompute vpd and re-filter | I9 | not tested (only AC-3.4, AC-3.5 and the disabled case) |
| AC-4.2 portion after a section with finds + final list sorted by vpd | I10 | not tested |
| AC-4.5 «осталось N подкатегорий» | I10 | budget stop is tested, the message is not |
| AC-5.2 single subcategory/card error → skipped, «ошибка» in summary, run continues | I9/I11 | only the 3-errors breaker is tested |
| AC-7.1 «уже было ДД.ММ» lookup from earlier runs (same `group_key` + `category_id`) | I4 `sort_finds` only | the DB lookup is not tested |
| AC-1.2 "уже идёт" **with progress** | I10 | the progress part is not asserted |

**Fix**: Add the missing checkboxes to I3/I5/I9/I10/I11 as listed. Also add a short AC → iteration → test-name table to the plan, so that I13's "AC traceability table" checks something that already exists instead of being written after the fact.

### MJ3: I10 bundles three concerns (discovery, run lifecycle, messaging) into one L iteration
**Iteration**: I10 (L): start/task, section discovery with a 30-day cache, crawl loop, budget, stop, resume, «already running», throttled progress, a new progress message on resume, portions, summary, «осталось N» and `mark_interrupted`. After MJ2 adds its checkboxes, that is around 15 done items.
**Problem**: Section discovery is its own concern. It makes live loads (≤25 on the first run), counts against the budget, and has its own persistence rule (upsert `categories`, refresh after 30 days). It has no done item today (see MJ2), and it is the iteration's only other user of `parse_subcategories` (I3). With three concerns in one L iteration, the review is weak exactly on the run-lifecycle logic.
**Fix**: Split it in two. **I9b (S)** `discover_sections()`: upsert, 30-day refresh, `REPORT_MAX_SUBCATS`, loads counted through `_before_load`; depends on 2, 3, 9. **I10 (M–L)** handles the lifecycle and messaging and depends on 5, 8, 9b. Update the dependency graph.

---

## MINOR Issues (approved with notes)

### N1: I1 assumes the provider can save raw pages, but it cannot yet
`AvitoBrowserProvider` exposes only `search()`, which returns parsed `Listing`s and never HTML. `fetch()` arrives in I7. **Fix**: say explicitly that the script uses `provider._ctx.new_page()` inside `async with provider` (the same launch arguments and profile), or raw Playwright with identical launch arguments. Make "≤ ~25 loads" a hard counter in the script. Spell out the safety rules: on a captcha or "Доступ ограничен", do not solve it, do not retry and do not run `app.auth`; close the browser, record the block in ADR-004 and hand back. Timebox I1, because the monitor is down for as long as it runs.

### N2: I1 question (a) has no stated method, and branch (b) = «no» changes a MUST AC
Raised listings carry no public marker. The only practical test is a card-page date that is newer than the seller-profile date for the same id, so (a) depends on (b). ADR-003 pre-approved only the "disable the profile check if the raise doesn't change the card date" branch. If the profile shows **no** dates, AC-3.1 cannot be met as written. That is a requirements change, so the orchestrator must take it to the user (human gate) before I3/I9 instead of the implementer recording a fallback in ADR-004. If (a) is inconclusive, the default is to keep `CHECK_SELLER_DATE=true`.

### N3: `Page` type has no home
I6 adds `AvitoProvider.fetch` to `base.py` but does not define `Page(html, title, final_url)`. I7 uses it, and I7's file list does not include `base.py`. **Fix**: define `Page` (a dataclass) in `base.py` in I6, so that the test `FakeProvider` and the crawler can import it without Playwright.

### N4: I8 "sleep between chunks" implies a dependency on I5
`split_message` lives in `market_logic` (I5). If the notifier splits text itself, I8 depends on I5 and is not wave-1 parallel. **Fix**: the notifier sends one chunk. The crawler (I10) loops over `split_message()` chunks with the 1 s delay, or the notifier takes `list[str]` and never imports `market_logic`. YAGNI on `edit_text`: catching «message is not modified» is enough. Drop the "skip unchanged" text cache, or keep just one of the two guards.

### N5: I4 is self-contradictory about m5
It says both "global per-subcategory ordering" and "keep section grouping as designed". Pick one; the design (§6) groups by section, and AC-4.2 portions rely on that. The design review asked for an explicit acceptance, so record it as a single line in `docs/decisions.md` (ADR-004 or ADR-005), not only in the plan.

### N6: Freeze the `MarketCrawler` constructor and loop skeleton early
I11 ‖ I12 is safe only if I11 does not change the constructor that I12 wires in `main.py`. **Fix**: I9 fixes the signature (`session_factory, provider_factory, notifier, settings, gate=None, clock=...`). I10 already writes the M1 top-level shape (`acquire` if a gate is present / `try` / `finally` close + release if owned) without handover, so that I11 only adds the `contended` branch in `_before_load` instead of rewriting the loop.

### N7: Load accounting gaps around M2
I11 counts the warm-up only on reopen. **Fix**: also count the initial warm-up at run start and at resume (+1, because the first `fetch` performs two gotos). Write `run.loads` on every exit path (stop/block/budget/failed in `finally`), not only in the per-subcategory commit. Otherwise the loads spent in a partially crawled subcategory are lost on `/stop`/block, which is the exact case M2 describes.

### N8: Effort header does not match the iterations
The header says "~4 S + 6 M + 3 L". The per-iteration sizes give one L (I10) plus I11 M–L, and I1 and I7 are S–M. I9 (paging, cuts, conditional second card, seller check, single commit, timeout, 7+ tests) is closer to M–L than M. Re-estimate it; the total of ≈35–45 h is plausible.

### N9: I12 AC-1.3 check is not testable as written
The scheduler is built inside `main()`, so "scheduler has only the monitor job (assert)" cannot be tested without a refactor. **Fix**: extract a tiny `build_scheduler(scanner)` used by `main()`, or verify AC-1.3 in code review (grep: no `add_job` other than `"scan"`). For `tests/test_handlers.py`, name the approach: `Dispatcher.feed_update` with a fake bot session, or thin handlers that call crawler methods tested elsewhere.

### N10: The I13 smoke run will not exercise the handover
The monitor interval is 60 min, while a 40-load run takes about 4–5 min, so no scheduled scan will contend. **Fix**: send `/check` during the run and verify that it answers within about one load. Commit before the smoke run.

### N11: Fixture hygiene
`market_seller.html` and the item pages contain third-party seller names and possibly phone fragments. Besides the existing grep for the user's own email/phone, strip or replace seller names and phones in the saved HTML (the parser tests do not need them). Alternatively, note that the repo has no remote and fixtures must not be published.

### N12: I1 bot-stop window vs Wave 1
With MJ1, I1 restarts the bot from the last green commit as soon as it finishes, independent of I2/I4/I6/I8. State this explicitly so that the monitor downtime is limited to I1's timebox.

---

## Design-review carry-over (all covered)

| Item | Iteration | Item | Iteration |
|---|---|---|---|
| M1 handover structure | I7 (`__aexit__` reset), I11 | m4 `date_checked` when disabled | I4 |
| M2 loads on run, not provider | I7, I9, I10, I11 (see N7) | m5 crawl_order grouping | I4 (see N5) |
| M3 views/page_date/seller_date | I2 | m6 preview, unchanged edit, title cap, chunk sleep, new progress on resume | I8, I5, I10 (see N4) |
| M4 breaker, `last_crawled_at` rule, ≤10 errors | I11, I5 | m7 403/429 → blocked | I7 |
| m1 stop rule + date formats | I3, I9 | m8 one commit per subcategory | I9 |
| m2 pick_cards counter | I4 | m9 `asyncio.timeout(120)` | I9 |
| m3 vpd age source | I4 | m10 profile-in-use retry | I11 |
| | | m11 doc hygiene + `REPORT_SECTIONS` default | I2, I13 |

## Assumptions about existing code: verified

- `AvitoProvider` is an ABC with `__aenter__/__aexit__` and abstract `search`. Adding a non-abstract `fetch` keeps the `FakeProvider` in `tests/test_scanner.py` working. OK.
- `AvitoBrowserProvider.__aexit__` does not reset `_ctx/_pw`, and `_warmed` persists (`avito_browser.py:26,44-48,54-58`). I7 fixes this as planned. OK.
- `Scanner` opens the provider at `scanner.py:75`. Wrapping only `provider_factory()` in `gate.hold()` with `gate=None` default keeps the 8 scanner tests untouched. OK.
- `TelegramNotifier.send_text` returns `None` and does not disable previews (`notifier.py:54-58`). The `Scanner.Notifier` Protocol tolerates `-> int | None`. OK.
- `matcher.normalize` exists for `norm_title`. `parse_published` lacks "25 сентября", "месяц назад" and absolute dates (`avito_parser.py:68-86`), which matches I3's list. `_dump()` exists for NFR-6. OK.
- `build_router(admin_chat_id, scanner, session_factory, scheduler)` and the chat filter at `handlers.py:23` cover AC-6.2 for free. OK.
- `init_db` uses `create_all`, so new tables are added without touching `watch_rules`/`listings` (NFR-5). OK.
- There are 23 tests, as claimed.

## YAGNI (single-user tool)

Proportionate overall: no Alembic, no repositories, no new dependencies, two new app modules. Small cuts: the `edit_text` text cache (N4). `scripts/save_fixtures.py` is worth keeping (not "throwaway") because it re-captures fixtures when Avito changes its markup (NFR-6), which costs nothing.

## What looks good

- Dependency graph matches the "Depends" fields, and the claimed parallel groups touch disjoint files (verified file by file, apart from the N4 coupling).
- Tests are woven into each iteration; there is no "tests" iteration at the end.
- The high-risk iteration (I11) has a blast radius, a rollback (`gate=None` in `main.py`), an opus assignment and a "23 original tests" criterion.
- I1 puts the live unknowns (raise vs date, profile date markup) first, before any parser code depends on them, with stop-on-block and an orchestrator-owned bot stop/restart.
- Fixtures-only tests (NFR-7), and the old monitor tests are never edited, only extended.
