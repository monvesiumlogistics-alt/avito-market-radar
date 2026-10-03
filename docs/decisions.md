# Decisions

## ADR-001: Requirements «Что выложить сегодня» approved (2026-10-03)
- Status: partly superseded (2026-10-03, requirements rev. 2: on-demand crawl of all Avito categories; awaiting re-approval)
- NO LONGER APPLIES: 09:00 schedule and catch-up report; tracked queries / search region per query; «снять»/«масштабировать» thresholds; expired-listings «перевыложить»; cabinet read.
- STILL APPLIES: China prices / margin out of v1, placeholder only.
- Open questions resolved with defaults: catch-up report ~5 min after start if 09:00 missed (before 23:00); search region = all Russia; «снять» = 0 new contacts 7 days and listing older than 7 days; «масштабировать» = ≥3 new contacts in 3 days; expired listings → separate «стоит перевыложить» top-5.
- China prices / margin: out of v1 (user decision), placeholder only.
- Cabinet read: once per day, read-only (user decision).

## ADR-002: Requirements rev.3 approved (2026-10-03)
- Status: accepted (user approved at requirements gate)
- On-demand only (/report), all Avito goods categories, sort by date with stop at first card >7 days, duplicate grouping (max 2 card pages per group), budget 600 loads/run with rotation.
- MIN_PRICE default 10 000 ₽ (user: cheaper items don't pay off after China cargo).
- Removed from v1: schedule, cabinet, «Новое на рынке», tracked queries, margin calculator.

## ADR-003: Run duration ~1 h accepted (2026-10-03)
- Status: accepted (user, design soft gate)
- 600 loads/run, 2–5 s pauses → ~55–65 min per run; NFR-2 target 40–50 min relaxed to ~65 min. Lower block risk preferred over speed.
- Design decisions kept as recommended: gate handover to D&G monitor between subcategories; seller-profile check verified on live pages in first iteration (disable if card date not changed by raise); pmin in search URL + local price re-check.

## ADR-004: Live verification spike (I1) — results (2026-10-03)
- Status: accepted. Pages captured live 2026-10-03 ~03:47 (orchestrator, 11 loads, no block after the user solved the captcha / IP block cleared). Earlier attempts: HTTP 439 captcha, then 429 «проблема с IP» on the first warm-up load; stopped, no retry (see ADR-005).
- Fixtures (sanitized, scripts stripped; DOM-only parsers don't need them): `tests/fixtures/market_search_s104.html` (50 cards, akkordeony, s=104), `market_section.html` (muzykalnye_instrumenty), `market_item.html`, `market_item_reserved.html`, `market_seller.html`, `market_seller2.html`. Re-capture: `scripts/save_fixtures.py`.
- (a) Seller profile DOES show per-listing dates: `[data-marker="item-date"]` inside `[data-marker^="item_list_with_filters/item("]` cards (carrying `data-item-id`), text relative, e.g. «5 часов назад». AC-3.1 is meetable; no human gate. Profile granularity is coarser/relative; same `parse_published` formats.
- (b) Raise: INCONCLUSIVE. For ids 8427557978 and 8306513242 the search (`Вчера`), card (`вчера в 22:36` / `22:34`) and profile (`5 часов назад` at ~03:47 = ~22:4x) dates all agree; no raised card was observed, so no evidence either way that a raise changes the card date. Decision: `CHECK_SELLER_DATE` stays true.
- (c) Formats and markup:
  - Search card date: `[data-marker="item-date"]` (same as existing parser SELECTORS["date"]); 49 of 50 cards say «Вчера» (no time!), one «1 час назад». Search date is therefore day-granular for older cards → the card page date is needed for real age. Existing `parse_published` returns None for bare «Вчера» (needs «Вчера HH:MM») — I3 must handle bare «вчера» (and «сегодня») as a day.
  - Card page: `[data-marker="item-view/item-id"]` «№ 8427557978», `item-view/item-date` «· вчера в 22:36» (lowercase, «в» before time), `item-view/total-views` «31 просмотр» / «50 просмотров» / «72 просмотра», `item-view/today-views` «(+14 сегодня)»; whole line: «№ 8427557978 · вчера в 22:36 · 31 просмотр (+14 сегодня)». Regex on text: `просмотр\w*` with `(\d[\d ]*)\s*просмотр`, `\+\s*(\d[\d ]*)\s*сегодня`; date regex must accept «вчера в HH:MM» (existing `(сегодня|вчера)\D*(\d{1,2}):(\d{2})` already does). Other item markers: `item-view/seller-info`, `item-view/item-price`, `item-view/title-info`.
  - Promo/reserved: «Забронировано» text inside the search card was seen (plus «Товар зарезервирован» on the card page); «Продвинуто» not present in this capture (kept in TEXT_PATTERNS from catalog.js, unverified live).
  - Selector diffs vs `avito_parser.py`: card/title/price/date selectors still match (parse_search_html → 50 listings). New markers needed: `item-view/*` above; profile cards use `data-marker="item_list_with_filters/item(N)"` (not `item`) so `SELECTORS["card"]` does not match profile pages — use `[data-item-id]`. Seller links are `/brands/<hash>` or `/user/<hash>` (names stripped in fixtures).
- (f) `crawl_order` is section-grouped as in tech-design §6 (plan N5/m5 accepted).

## ADR-005: Captcha → human solves it, crawl pauses (2026-10-03)
- Status: accepted (explicit user request)
- Context: spike I1 hit «Доступ ограничен: проверка безопасности» (HTTP 439) on warm-up; user solved the captcha manually in the bot's browser window (`python -m app.auth`) and asked for this flow.
- Decision: bot never solves/bypasses captcha. On captcha/block page during /report: send Telegram alert «🧩 Avito просит капчу — пройди её в окне браузера бота», pause the crawl (browser stays open, HEADLESS=false), poll the page every ~30 s for up to CAPTCHA_WAIT_MINUTES (default 30); when the block page is gone → continue; on timeout → stop as before (save state, send collected finds). Supersedes the "stop immediately on block" part of AC-5.x for /report. Monitor (D&G) behaviour unchanged (alert + skip).
- Plan impact: implement in I11 (block handling); requirements AC-5.1 to be updated accordingly.
- Implemented (I11): `MarketCrawler._fetch` catches `ProviderBlocked`, sends the alert (with the wait in minutes), and calls `provider.wait_unblocked(url, CAPTCHA_WAIT_MINUTES*60, poll 5 s)`: a separate tab on the blocked URL is polled until `is_blocked` is false; the load is then retried once (counts as a load). Timeout, `/stop` during the wait, `HEADLESS=true` or `CAPTCHA_WAIT_MINUTES=0` -> no wait; status `blocked`, finds kept, summary sent, resume via /report within 12 h. Requires `HEADLESS=false`. The browser (and the BrowserGate) stays held during the wait, so a monitor scan waits too (worst case CAPTCHA_WAIT_MINUTES). Monitor behaviour unchanged (alert + skip).

## ADR-006: Seed the category map from catalog.csv, fallback subcategory parser (2026-10-03)
- Status: accepted (user request after the live smoke run)
- Context: live discovery found no subcategories for noutbuki, remont_i_stroitelstvo, mebel_i_interer (the rubricator selector misses their markup). The user wants the full Avito map first, then crawling by it.
- Decision: (1) `parse_subcategories` falls back, when the rubricator gives nothing, to the `catalog.js` rule: any `<a>` whose path is `/<x>/<section>/<sub>`, not an item (`_\d{7,}`), text < 40 chars, normalized to `/rossiya/<section>/<sub>`. (2) `scripts/import_map.py` upserts `categories` from the read-only `avito_niche/catalog.csv` (url = BASE_URL + key, `discovered_at` = now, so discovery skips those sections for 30 days) and stores the CSV `score` as `categories.prior_score`. (3) `crawl_order`: never-crawled categories go first, in descending `prior_score`; crawled ones keep the days-since-crawl x best-vpd weight. Nothing is filtered out by CSV prices. (4) `init_db` adds the `prior_score` column to an existing SQLite DB with an idempotent `ALTER TABLE` (create_all does not alter tables).
- Run: `PYTHONIOENCODING=utf-8 .venv/Scripts/python -m scripts.import_map [path]`.

## ADR-007: Fix-pass after final review (2026-10-03)
- Status: accepted (orchestrator; the user is asleep and delegated all decisions)
- Context: final review (docs/code-review.md, docs/security-review.md, docs/test-report.md) found one HIGH, several MEDIUM and LOW items.
- Decisions:
  1. `Find.copies/price_min/price_max` come from the FULL group; `pick_groups`/`pick_cards` only choose which cards to open (max 2 per group, the 3rd copy stays unopened when both opened are low).
  2. `BrowserLost` (providers/base.py) is raised when the browser cannot be reopened after a gate handover or is missing; `_open_card`/`_seller_date` re-raise it. A subcategory where every opened card failed raises an error, so it counts as an error for the breaker (no `last_crawled_at`).
  3. Resume: each `/report` press starts with a fresh `REPORT_BUDGET`; `run.loads` stays a cumulative statistic (`MarketCrawler._add_loads`).
  4. The final summary sends a totals line (found, hot, subcategories, loads) plus only the finds not yet sent in portions; no duplicates.
  5. `is_blocked`: firewall markup anywhere in the HTML; the words «Доступ ограничен»/«Вы робот» only in `<title>` and the first 3000 chars of visible text.
  6. `wait_unblocked`: wall-clock deadline (`loop.time()`), `tab.content()/title()` wrapped in `asyncio.wait_for(PROBE_TIMEOUT=15)`, `/stop` checked around every sleep.
  7. `split_message`: flush the current chunk before cutting a long line, prefer `", "` then a space, never cut inside a tag or `&entity;`.
  8. `html.escape` for dynamic text in Telegram (block alert, section names); `is_avito_url` allowlist (`avito.ru` + subdomains) for card, seller and subcategory links.
  9. `scripts/save_fixtures.sanitize` masks review buyer names and texts; `market_seller*.html` re-sanitized in place.
  10. LOW: batched `_lines`; `_finish` wrapped (the summary is sent even if the DB write fails); `CrawlRun.note` removed from the model (the column stays in old DBs); the progress message is sent once and not retried every minute on failure; Moscow time (`msk_now`, zoneinfo + tzdata on Windows) for relative dates and the crawler clock; «29 февраля» resolves to the previous leap year; `link_preview_options` instead of `disable_web_page_preview`.
  11. Section display names: static `SECTION_NAMES` (25 slugs) in `market_logic`.
  12. Docs: AC-5.1 refers to ADR-005, AC-5.3 states the captcha-wait exception, AC-2.4a/4.2/4.4 updated, tech-design cleaned up (section 12).
- Not done: `BrowserGate.locked` kept (used by tests); `fetch` abstract change skipped (non-abstract default is harmless); thin wrappers in `market_logic` kept (testability).

## ADR-008: Roadmap batch F1-F5 (2026-10-03)
- Status: accepted (orchestrator; the user delegated decisions)
- F1 goofish link: `goofish_query(title)` keeps Latin tokens (letters/digits/-/.; at least one letter; length >= 2), drops generic words (new, original, orig, size, cm, mm, kg, set, lot), max 4 tokens in order, None without a model. `format_find` appends `🔎 goofish` (urlencoded + escaped). Tokens without a Latin letter (e.g. «15» in «iPhone 15») are dropped by design.
- F2 `/top [days]`: DB only (no Avito loads), dedup by `group_key` keeping the best vpd, sorted by vpd, max 15, `split_message`.
- F3 `/export`: CSV (utf-8-sig, `;`) as a document; cells starting with `= + - @` get a leading `'` (formula injection in Excel).
- F4 `Category.skipped` (idempotent `ALTER`, generalized `_ADDED_COLUMNS` in `init_db`). `/cats`, `/skip text`, `/unskip text` match slug, Russian section name or subcategory name case-insensitively in Python (SQLite `lower` does not handle Cyrillic), minimum 2 chars. `crawl_order` and `MarketCrawler._categories` exclude skipped categories (progress totals too).
- F5 feedback: simplest variant. A portion with <= 8 finds gets numbered lines and an inline keyboard, one row per find (`👍 n`/`👎 n`, callback `fb:<find_id>:<1|-1>`); the callback sets `Find.feedback` (overwrite, not accumulate). Callbacks are filtered to the admin chat. `crawl_order(..., feedback)` multiplies priority by `feedback_factor(net)` = clamp(1 + 0.5 * net, 0.25, 2.5), net = sum of feedback over the category's finds. Portions with more than 8 finds and the final summary have no buttons. `send_text` got an optional `markup` argument (only passed when a keyboard exists).

