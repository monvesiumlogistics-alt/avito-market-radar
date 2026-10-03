# Test Report: Market crawl (/report), requirements rev.3 + ADR-005 + ADR-006

**Date**: 2026-10-03
**Test run**: PASS, 121 passed, 0 failed (`pytest -q`, 12.7 s). Audit only, no tests added. No network, app not run.
Stack: pytest + pytest-asyncio, fakes for provider / notifier / Telegram session, real SQLite (tmp file or memory), HTML fixtures in `tests/fixtures/`.

## Coverage Summary

| Layer | Files | Notes |
|---|---|---|
| Unit: pure logic (`market_logic`) | test_market_logic.py (15) | age/vpd, grouping, picking, order, sort, formatting |
| Unit: parser on fixtures | test_parser.py (17) | search, subcategories (+fallback), item page, seller date |
| Integration: crawler + real DB + fake provider | test_market.py (~48) | pagination, filters, seller check, errors, lifecycle, gate, block |
| Integration: Telegram handlers | test_handlers.py (3) | aiogram Dispatcher with fake session |
| DB / config / import | test_db_market.py, test_config.py, test_import_map.py, test_main.py | |
| Provider | test_provider.py (6) | fetch + block detection only |
| E2E | none | not applicable (Telegram bot, live Avito forbidden) |

## Acceptance Criteria Coverage

| AC | Prio | Tests | Status |
|---|---|---|---|
| AC-1.1 | MUST | `test_handlers::test_report_and_stop_commands_and_buttons`, `test_market::test_start_new` | COVERED |
| AC-1.2 | MUST | `test_already_running_shows_progress` | COVERED |
| AC-1.3 | MUST | `test_main::test_scheduler_only_scan` (only the `scan` job exists) | COVERED |
| AC-2.1 | MUST | `test_config::test_report_defaults_and_env_override` (25 sections, env override); `test_parser::test_parse_subcategories` (cap 18/3); `test_market::test_discover_upsert_no_duplicates_on_rerun`, `test_refresh_30d_fresh_not_reloaded_stale_reloaded`, `test_max_subcats_and_loads_counted`, `test_discover_error_skipped_other_sections_continue` | COVERED |
| AC-2.2 | MUST | `test_crawl_stops_on_old`, `test_promo_no_stop`, `test_max_pages_and_days_covered_when_page_limit`, `test_parser::test_search_fixture_bare_yesterday_and_promoted`. s=104 asserted only via URL in `test_price_cut_local_with_pmin` and the `s=104` fixture key. Reserved cards: only in the item-page parser, not skipped-in-search | PARTIAL (reserved-card skip in search untested; `item-date` selector exercised only through the fixture) |
| AC-2.3 | MUST | `test_max_pages_and_days_covered_when_page_limit` (last_days_covered), `test_market_logic::test_summary_covered_days_remaining_and_error_cap` ("Покрыто не полностью: N из 7") | COVERED |
| AC-2.4 | MUST | `test_price_cut_local_with_pmin`, `test_old_age_cut_by_search_date`, `test_crawl_stops_on_old`, `test_market_logic::test_is_find_is_hot` | COVERED |
| AC-2.4a | MUST | `test_second_card_only_if_first_low_vpd`, `test_market_logic::test_group_cards_price_spread`, `test_pick_cards_oldest_groups_and_cards_first`, `test_norm_title`. Not tested: a 3rd copy is NOT opened when both of two have low vpd (the crawler loop; `pick_cards` returns 2 cards, so this holds by design but no test pins it) | PARTIAL |
| AC-2.4b | MUST | `test_find_persists_views_page_date_seller_date_through_crawler`, `test_db_market::test_find_persists_views_page_date_seller_date`, `test_parser::test_parse_item_page` | COVERED |
| AC-2.4c | MUST | `test_cap_12_opened_pages_with_fixture`, `test_market_logic::test_pick_cards_no_slot_reservation_m2`, `test_pick_cards_oldest_groups_and_cards_first` | COVERED |
| AC-2.5 | MUST | `test_is_find_is_hot`, `test_find_persists_...through_crawler` (hot), `test_seller_checked_recompute_vpd` (not hot at 75) | COVERED |
| AC-2.6 | MUST | `test_market_logic::test_format_find_fields`, `test_format_find_plural_and_min_age`, `test_format_find_hostile_title_escaped_and_capped`, `test_market::test_portion_after_section_and_final_sorted`, `test_second_card_only_if_first_low_vpd` (copies) | COVERED |
| AC-3.1 | MUST | `test_find_persists_...through_crawler`, `test_seller_checked_recompute_vpd`, `test_parser::test_parse_seller_date` (profile opened only for candidates: `test_item_without_views_counter_skipped`, `test_stale_card_skips_profile`) | COVERED |
| AC-3.2 | MUST | `test_seller_date_old_drops_find` | COVERED |
| AC-3.3 | MUST | `test_seller_checked_recompute_vpd`, `test_seller_recompute_can_drop_below_vpd_min` | COVERED |
| AC-3.4 | MUST | `test_seller_not_found_unchecked` (not found and profile exception), `test_market_logic::test_date_checked_when_seller_check_disabled` | COVERED |
| AC-3.5 | MUST | `test_stale_card_skips_profile` | COVERED |
| AC-4.1 | MUST | `test_progress_once_per_minute`, `test_market_logic::test_format_progress`, `test_notifier::test_edit_text_*` | COVERED |
| AC-4.2 | MUST | `test_portion_after_section_and_final_sorted`, `test_failed_telegram_portion_stays_unsent_but_in_summary` | COVERED |
| AC-4.3 | MUST | `test_stop_summary`, `test_stop_command_replies`, `test_handlers` (stop wiring) | COVERED |
| AC-4.4 | MUST | `test_resume_lt_12h_no_repeats` (11 h, "Продолжаю"), `test_new_run_gt_12h` (13 h), `test_mark_interrupted` (restart/PC off), `test_interrupted_subcat_no_duplicates_and_loads_kept` | COVERED |
| AC-4.5 | MUST | `test_budget_stop_remaining_msg_and_loads_persist`, `test_budget_and_block_propagate_and_persist_loads` | COVERED |
| AC-4.6 | MUST | `test_market_logic::test_crawl_order`, `test_crawl_order_never_crawled_by_prior_score` | COVERED |
| AC-5.1 (as changed by ADR-005) | MUST | `test_block_midrun_resumable` (alert, wait invoked with 15 min, status blocked, finds kept, summary, resume), `test_block_human_passes_crawl_continues`, `test_block_headless_no_wait`, `test_parser::test_block_page_detected`, `test_block_marker_robot`, `test_provider::test_fetch_blocked_*`, `test_status_blocked`. Not tested: real `AvitoBrowser.wait_unblocked` (polling, timeout, cancel); `/stop` during the wait; `CAPTCHA_WAIT_MINUTES=0`; load retry counted after the human passes. Fake provider only returns a boolean | PARTIAL |
| AC-5.2 | MUST | `test_subcat_error_skipped_in_summary`, `test_card_error_skipped_run_continues`, `test_fetch_timeout_counted_as_error`, `test_breaker_three_errors_failed_one_summary` | COVERED |
| AC-5.3 | MUST | `test_gate_held_during_run_and_released`, `test_scan_gets_gate_within_one_load_and_handover_loses_nothing`, `test_reopen_retry_after_profile_in_use`, `test_reopen_error_after_release_no_runtime_error_and_cause_kept`, `test_scanner::test_scan_waits_gate`, `test_gate_contended_only_while_waiter_exists`. Caveat: during an ADR-005 captcha wait the gate stays held (monitor waits up to CAPTCHA_WAIT_MINUTES, 30 min default, which exceeds the 15 min limit); not tested and not reflected in the AC | PARTIAL |
| AC-5.4 | MUST | `test_crawler_crash_monitor_unaffected`, `test_crash_marks_failed_and_releases_gate`, plus the unchanged `test_scanner.py` suite (all green) | COVERED |
| AC-6.1 | SHOULD | `test_handlers::test_start_buttons`, `test_report_and_stop_commands_and_buttons` | COVERED |
| AC-6.2 | SHOULD | `test_handlers::test_foreign_chat_ignored` | COVERED |
| AC-7.1 | SHOULD | `test_already_seen_from_db_marks_date`, `test_sort_finds_hot_then_new_then_vpd`, `test_format_find_fields` ("уже было 01.10") | COVERED |
| AC-8.1 | COULD | `test_db_market::test_finds_china_price_nullable` (nullable, empty); "not shown" not asserted explicitly, but `format_find` tests show no such field | COVERED |
| ADR-006 (a) subcategory fallback parser | n/a | `test_parser::test_parse_subcategories_fallback_without_rubricator` | COVERED |
| ADR-006 (b) `import_map` upsert, prior_score, discovered_at | n/a | `test_import_map::test_import_map_upsert_idempotent` (BOM, empty key skipped, history kept) | COVERED |
| ADR-006 (c) never-crawled first by prior_score | n/a | `test_crawl_order_never_crawled_by_prior_score` | COVERED |
| ADR-006 (d) idempotent ALTER TABLE | n/a | `test_import_map::test_prior_score_column_added_to_existing_table` | COVERED |

## NFR spot check

| NFR | Status |
|---|---|
| NFR-1 budget 600 default | COVERED (`test_report_defaults...`, budget tests). The random 2-5 s pause (`random.uniform(*self.delay)`) is untested. |
| NFR-6 save HTML to `data/debug/` on 0 elements | MISSING (`avito_browser.py:124` writes it; no test) |
| NFR-7 tests on HTML fixtures, no live Avito | COVERED |

## Summary

- COVERED: 24 of 28 AC rows (AC-1.1..1.3, 2.1, 2.3, 2.4, 2.4b, 2.4c, 2.5, 2.6, 3.1..3.5, 4.1..4.6, 5.2, 5.4, 6.1, 6.2, 7.1, 8.1) plus all 4 ADR-006 items.
- PARTIAL: 4 (AC-2.2, AC-2.4a, AC-5.1 under ADR-005, AC-5.3).
- MISSING: 0 ACs entirely. No MISSING MUST AC.

## Known Gaps (ordered by risk)

1. AC-5.1 / ADR-005 (MUST): `AvitoBrowser.wait_unblocked` is untested (poll loop, timeout, `cancel` on /stop). It is the riskiest new code and is only faked.
2. AC-5.1: no test for `/stop` during the captcha wait, `CAPTCHA_WAIT_MINUTES=0`, or the retried load counted after the human passes.
3. AC-5.3: captcha wait holds the BrowserGate, so a monitor scan can be delayed up to 30 min, above the 15 min AC. Requirements should state the exception (the ADR says this, the AC text does not), then add a test.
4. AC-2.2: no test that reserved ("Забронировано") search cards are skipped without stopping the crawl; `item-date` selector covered only via the fixture.
5. AC-2.4a: no crawler test that the 3rd copy stays unopened when the first two are low vpd.
6. NFR-6: debug HTML dump on 0 parsed elements untested.
7. Doc drift: `requirements.md` AC-5.1 still says "stop immediately"; ADR-005 asks to update it.
8. Quality notes: some tests carry several assertions (style rule), and the suite uses fakes for the provider, which is fine here because live Avito is off-limits. No flaky tests seen in one run (not repeated 3 times).

## Quality Verdict

APPROVED WITH CONDITIONS. All MUST ACs have at least one test, none are fully MISSING, and 121 of 121 tests pass. Conditions: add tests for gaps 1 and 2 (captcha wait on the real provider and /stop during the wait) before relying on ADR-005 in live runs, and resolve gap 3 and the doc drift in item 7.
