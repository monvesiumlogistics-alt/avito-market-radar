# Implementation Plan: «Что выложить» (проверка рынка по кнопке)

**Design**: docs/tech-design.md · **Design review**: docs/design-review.md (M1–M4, m1–m11 folded in; design doc patched only in I13) · **Plan review**: docs/plan-review.md · **Requirements**: docs/requirements.md rev.3
**Iterations**: 14 (I1–I13 + I9b) · **Effort**: ≈ 38–48 h (S: 1,2,5,6,8,9b,13 · M: 3,4,7,12 · M–L: 9,10,11) · **Approved by**: (after plan gate)

## Execution rules
1. **Strictly sequential, one working tree, no parallel implementers.** Run in the order below; "Depends" is the dependency order.
2. **Commit the baseline (docs/ incl. plan, NEXT.md) before I1.** Commit after every green iteration (`git commit`, no push; repo has no remote).
3. Gate for every iteration: `.venv/Scripts/python -m pytest` green (23 old tests untouched + new) and `.venv/Scripts/ruff check .` clean.
4. The live D&G bot is restarted only from a green committed boundary: right after I1 (it changes no `app/` code, so monitor downtime = I1 timebox), after I7 (live-path change; check one scan), and after I13.
5. Fixtures never leave the repo (no remote); seller names/phones are stripped (N11). Model: sonnet; opus only for I11.

## Dependency order
```
I1 → I2 → I3 → I4 → I5 → I6 → I7 → I8 → I9 → I9b → I10 → I11 → I12 → I13
real deps: I3←I1 · I5←I4 · I7←I6 · I9←I2,I3,I4,I7 · I9b←I2,I3,I9 · I10←I5,I8,I9b · I11←I6,I10 · I12←I10 · I13←I11,I12
```

## AC → iteration → test (every MUST AC)
| AC | Iter | Test |
|---|---|---|
| 1.1, 1.2 (+progress), 1.3 | 10, 12 | test_market::test_start_new/test_already_running_shows_progress; test_main::test_scheduler_only_scan |
| 2.1 | 9b | test_market::test_discover_upsert/refresh_30d/max_subcats |
| 2.2 | 9 | test_crawl_stops_on_old/promo_no_stop/max_pages |
| 2.3 | 5, 9 | test_days_covered_when_page_limit; test_summary_covered_days |
| 2.4 | 9 | test_price_cut_local_with_pmin/old_age_cut |
| 2.4a / 2.4c | 4, 9 | test_pick_cards; test_second_card_only_if_low_vpd/cap_12 |
| 2.4b | 2, 9 | test_find_persists_views_page_date_seller_date |
| 2.5, 2.6 | 4, 5 | test_is_find_hot; test_format_find_fields |
| 3.1–3.5 | 9 | test_seller_checked/drop_old/recompute_vpd/not_found_unchecked/stale_card_skips_profile |
| 4.1, 4.2 | 10 | test_progress_once_per_minute; test_portion_after_section/final_sorted |
| 4.3, 4.4, 4.5, 4.6 | 10, 4 | test_stop_summary; test_resume_lt_12h/new_run_gt_12h; test_budget_remaining_msg; test_crawl_order |
| 5.1, 5.2 | 11, 9 | test_block_midrun_resumable; test_subcat_error_skipped_in_summary |
| 5.3 | 6, 11 | test_scan_waits_gate; test_scan_gets_gate_within_one_load |
| 5.4 | 11 | test_crawler_crash_monitor_unaffected + 23 old tests |
| 6.1, 6.2 | 12 | test_handlers::test_start_buttons/foreign_chat_ignored |
| 7.1 | 10 | test_already_seen_from_db_marks_date |
| 8.1 | 2 | test_finds_china_price_nullable |

---

## Iteration 1: Live verification spike  (S–M)
**Goal**: real fixtures saved; ADR-004 answers the open questions.
**Orchestrator stops the D&G bot first (it holds `data/avito_profile`), restarts it from the last green commit right after I1.** Implementer waits for the stop confirmation and closes its browser before handing back. Timebox ≤ 1 h (monitor is down meanwhile).
**Create**: `scripts/save_fixtures.py` (kept: re-capture when markup changes; uses `AvitoBrowserProvider` launch args via `async with provider` + `provider._ctx.new_page()`, since `fetch()` arrives in I7; hard counter ≤ 25 loads, pauses 2–5 s), `tests/fixtures/market_search_s104.html` (with promoted card, mixed date formats), `market_section.html`, `market_item.html`, `market_item_raised.html`, `market_seller.html`.
**Modify**: `docs/decisions.md` (ADR-004).
**Safety**: on captcha / «Доступ ограничен»: no solving, no retry, no `app.auth`; close the browser, record the block in ADR-004, hand back.
**Work / ADR-004 answers**: (a) card-page date vs seller-profile date for the same id (a card newer than its profile entry = raised); inconclusive → keep `CHECK_SELLER_DATE=true`; (b) does the profile show the listing's date, and which markup; (c) date formats seen; (d) promo/reserved marker text; (e) card «N просмотров (+M сегодня)» markup; (f) line recording the ordering decision from I4 (section-grouped `crawl_order` accepted, m5).
**Done**: [ ] 5 fixtures exist, non-empty, grep clean of the user's email/phone [ ] seller names and phones stripped from saved HTML [ ] ADR-004 has a–f [ ] **if (b) = profile shows no listing date → AC-3.1 unmeetable: implementer stops, orchestrator raises a human gate (requirements change) before I3/I9** [ ] bot restarted by orchestrator [ ] pytest/ruff unchanged green, committed.
**Depends**: none · **Risk**: medium (live block risk).

## Iteration 2: Config + DB tables  (S)
**Modify**: `app/config.py` (all §8 vars; `REPORT_SECTIONS` defaults in code to the 25 `TOP` slugs, env only overrides — m11), `app/db.py` (`categories`, `crawl_runs`, `finds` with `views`, `page_date`, `seller_date`, `china_price` — M3), `.env.example`, `tests/test_config.py`. **Create**: `tests/test_db_market.py`.
**Done**: [x] defaults parse, env override works [x] `create_all` adds 3 tables, `watch_rules`/`listings` unchanged (NFR-5) [x] `test_find_persists_views_page_date_seller_date` (row round-trip) and `test_finds_china_price_nullable` [x] gate.
**Depends**: none · **Risk**: low–medium (shared `db.py`).

## Iteration 3: Parser extensions  (M)
**Modify**: `app/providers/avito_parser.py` (`SELECTORS`, `TEXT_PATTERNS`, `BLOCK_MARKERS` += «Вы робот»; `promoted_ids`, `parse_subcategories`, `parse_item_page -> ItemStats`, `parse_seller_date`; `parse_published` += «25 сентября», «25 сентября 2025», «1 неделю назад», «месяц / N месяцев назад» as old — m1), `tests/test_parser.py`.
**Done**: [x] each function tested on the I1 fixtures [x] `parse_subcategories` respects `limit` [x] unparsable date → None, never raises [x] item page without views counter → None fields [x] old parser tests unchanged [x] gate.
**Depends**: 1 · **Risk**: medium (parser shared with monitor).

## Iteration 4: market_logic core  (M)
**Create**: `app/services/market_logic.py`, `tests/test_market_logic.py`.
**Work**: `age_days` (min 1), `norm_title` (XDJ-RX2 ≠ XDJ-RX3), `group_cards` (±20 %), `pick_cards` as ordered groups + running opened-page counter capped at `REPORT_CARDS_PER_SUBCAT` (m2), `is_find`/`is_hot`, vpd age = card-page date with search date as fallback (m3), `date_checked` true when seller check disabled (m4), `crawl_order` **section-grouped, as designed §6 (m5 accepted; one line to decisions.md in I1/ADR-004)**, `sort_finds` with «уже было».
**Done**: [x] table tests for each function incl. m2/m3/m4 [x] `test_crawl_order` (never-crawled first, best_vpd weight, sections grouped, done-in-run excluded) [x] gate.
**Depends**: none · **Risk**: low.

## Iteration 5: Formatting  (S)
**Modify**: `market_logic.py` (`format_find` with `html.escape`, title cap ~120, `format_progress`, `format_summary` with ≤10 errors + «и ещё N», covered days per category, «осталось N подкатегорий», `split_message`), `tests/test_market_logic.py`.
**Done**: [x] `test_format_find_fields`: price range, best vpd, «+сегодня», age or «дата не проверена», «выставлено N раз», category, «уже было ДД.ММ» [x] `test_summary_covered_days`, remaining-count line, error cap [x] no chunk > 4096, tags intact [x] hostile title escaped/capped [x] gate.
**Depends**: 4 · **Risk**: low.

## Iteration 6: BrowserGate, Page, scanner hook  (S)
**Modify**: `app/providers/base.py` (`Page(html,title,final_url)` dataclass — N3; `BrowserGate`: Lock + waiter count, `hold()`, `acquire/release`, `locked`, `contended`; non-abstract `AvitoProvider.fetch` raising `NotImplementedError`), `app/services/scanner.py` (`gate=None`; `async with gate.hold()` around `provider_factory()` only), `tests/test_scanner.py` (+gate tests).
**Done**: [x] `gate=None` path identical, old scanner tests green [x] `test_scan_waits_gate` [x] `contended` true while a waiter exists [x] gate.
**Depends**: none · **Risk**: medium (`scanner.py`, AC-5.4). Rollback: revert the param.

## Iteration 7: Provider `fetch()`  (S–M)
**Modify**: `app/providers/avito_browser.py` (`fetch(url, ready_selector=None) -> Page`: warm-up, pause, `is_blocked`, goto 403/429 → `ProviderBlocked` (m7); `__aexit__` resets `_ctx/_pw/_warmed`, idempotent (M1); no provider load counter (M2); `search()` thin wrapper), `tests/test_provider.py` (status→blocked helper; double `__aexit__` with a stub).
**Done**: [x] existing tests green, `search()` behaviour unchanged [x] double `__aexit__` safe [x] gate, committed [ ] orchestrator restarts bot, one D&G scan verified.
**Depends**: 6 · **Risk**: medium (live monitor path).

## Iteration 8: Notifier additions  (S)
**Modify**: `app/services/notifier.py` (`send_text -> int|None`, `disable_web_page_preview=True`; `edit_text` swallows «message is not modified», no text cache; notifier sends ONE chunk and never imports `market_logic` — N4), `tests/test_notifier.py` (new, fake bot).
**Done**: [ ] preview disabled [ ] «not modified» swallowed [ ] other errors logged, not raised [ ] gate.
**Depends**: none · **Risk**: low.

## Iteration 9: crawl_subcategory + crawler skeleton  (M–L)
**Create**: `app/services/market.py`, `tests/test_market.py` (FakeProvider.fetch URL→HTML, in-memory SQLite, fake notifier).
**Work**: freeze `MarketCrawler(session_factory, provider_factory, notifier, settings, gate=None, clock=...)` (N6); paging `s=104&pmin`, stop rule = finish current page after first old non-promoted card (m1), `days_covered`, price/age cut with local price re-check, group+pick, card parse, seller check only on fresh card date (AC-3.5), vpd recompute; **one commit per subcategory** (finds + category + `run.loads`, m8); `asyncio.timeout(120)` per fetch → card/subcat error (m9); `_before_load` hook: stop/budget checks, increments `run.loads` (M2).
**Done**: [ ] test_crawl_stops_on_old, promo_no_stop, max_pages [ ] test_price_cut_local_with_pmin, old_age_cut [ ] test_days_covered_when_page_limit [ ] copies open ≤2 cards, second card only if first low vpd, cap 12 [ ] test_seller_checked/drop_old/recompute_vpd/not_found_unchecked/stale_card_skips_profile, seller check disabled → `date_checked` true [ ] test_find_persists_views_page_date_seller_date through the crawler [ ] test_subcat_error_skipped_in_summary (card error and subcat error, run continues) [ ] crash between writes → no duplicate finds [ ] fetch timeout counted as error [ ] gate.
**Depends**: 2,3,4,7 · **Risk**: medium.

## Iteration 9b: discover_sections  (S)
**Modify**: `market.py` (`discover_sections()`: upsert `categories`, refresh when older than 30 days, `REPORT_MAX_SUBCATS`, loads via `_before_load`), `tests/test_market.py`.
**Done**: [ ] test_discover_upsert (no duplicates on rerun) [ ] test_refresh_30d (fresh not reloaded, stale reloaded) [ ] test_max_subcats [ ] discovery loads counted in `run.loads` [ ] gate.
**Depends**: 2,3,9 · **Risk**: low.

## Iteration 10: Run lifecycle and messaging  (M–L)
**Modify**: `market.py`, `tests/test_market.py`.
**Work**: `start()`/task; top-level run shape without handover (`acquire` if gate present / `try` / `finally`: close provider, release if owned — N6); count initial warm-up at start/resume (+1, N7); write `run.loads` on every exit path in `finally` (N7); loop in `crawl_order`; budget; `stop_requested`; resume < `REPORT_RESUME_HOURS` else new run; «already running» + progress; progress edit ≤1/min (injectable clock), new progress message on resume (m6); portion after a section with finds; final summary; «осталось N подкатегорий»; chunk loop over `split_message` with 1 s delay (clock injectable, 0 in tests); `mark_interrupted()`; DB lookup for «уже было».
**Done**: [ ] test_start_new, test_already_running_shows_progress [ ] test_budget_stop_loads_persist_across_resume + test_budget_remaining_msg [ ] test_stop_summary (status `stopped`, loads kept from partial subcat) [ ] test_resume_lt_12h (no repeats) / new_run_gt_12h [ ] test_progress_once_per_minute [ ] test_portion_after_section, test_final_summary_sorted [ ] test_already_seen_from_db_marks_date [ ] test_mark_interrupted [ ] gate.
**Depends**: 5,8,9b · **Risk**: medium.

## Iteration 11: Gate handover, block, failure breaker  (M–L, opus)
**Modify**: `market.py`, `tests/test_market.py`, `tests/test_scanner.py`.
**Work**: add only the `contended` branch to `_before_load` (loop untouched, N6): `__aexit__` old provider, release, re-acquire, **new** provider (fresh warm-up, +1 load), one retry after 2 s on «profile in use» (m10). `ProviderBlocked` → alert + summary + `blocked`. Breaker: 3 consecutive subcategory errors → `failed`; an errored subcat sets `last_run_id` but not `last_crawled_at` (M4).
**Done**: [ ] test_scan_gets_gate_within_one_load [ ] error between release and re-acquire raises neither `RuntimeError` nor hides the cause [ ] handover mid-subcategory loses nothing [ ] test_block_midrun_resumable [ ] 3 errors → `failed`, one summary, errors capped [ ] test_crawler_crash_monitor_unaffected [ ] 23 original tests pass [ ] gate.
**Depends**: 6,10 · **Risk**: high. Blast radius: monitor starvation. Rollback: `gate=None` in `main.py`.

## Iteration 12: Telegram commands + wiring  (M)
**Modify**: `app/telegram/handlers.py` (`build_router(..., crawler)`, `/report`, `/stop`, ReplyKeyboard in `/start`, buttons via `F.text`), `app/main.py` (one `BrowserGate` shared by Scanner and MarketCrawler; `mark_interrupted` on boot; extract `build_scheduler(scanner)` used by `main()` — N9). **Create** `tests/test_handlers.py` (via `Dispatcher.feed_update` with a fake bot session or thin handlers calling crawler fakes), `tests/test_main.py`.
**Done**: [ ] replies per §7 table [ ] test_start_buttons [ ] test_foreign_chat_ignored (AC-6.2) [ ] test_scheduler_only_scan (jobs == {"scan"}, AC-1.3) [ ] gate.
**Depends**: 10 · **Risk**: medium (`main.py` startup).

## Iteration 13: Doc hygiene + live smoke  (S)
**Modify**: `docs/tech-design.md` (m11: stale NFR-2 note, §11 marked resolved, M1–M4 corrections), README run notes if present.
**Work (orchestrator; commit first, bot stopped/restarted)**: `/report` with `REPORT_BUDGET=40`; send `/check` during the run and confirm it answers within ≈ 1 load (a 60-min scan interval will not contend by itself — N10); `/stop` then resume.
**Done**: [ ] smoke ends with a summary, no block [ ] `/check` served mid-run [ ] AC table above matches existing test names [ ] gate.
**Depends**: 11,12 · **Risk**: medium (live).
