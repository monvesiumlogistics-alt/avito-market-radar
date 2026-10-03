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

## ADR-009: goofish query fix, model aggregation, margin (2026-10-03)
- Status: accepted (orchestrator; the user delegated decisions)
- G1: `goofish_query` keeps number tokens that sit right next to an accepted Latin token («iPhone 15 Pro Max», «DDJ 400»); 4-digit years (19xx/20xx) are dropped; max 4 tokens. Nothing is added to the title: the earlier «Apple iPhone Pro Max» came from my own test title that already contained «Apple», not from brand injection; the number «15» was dropped because of the old "needs a Latin letter" rule, now fixed.
- G2: `model_key(title)` = first 2 Latin tokens (+ numbers between/directly after them), lowercased; None unless the key has a digit or 2 Latin words. All 4 real night titles («Aimiko u2 pro premium 3000w» etc.) give `aimiko u2`: version words (pro/premium/power) are not part of the key because they are variants of one model whose spread is shown as vpd and price ranges. Known limit: «Nike Air Max» and «Nike Air Force» both give `nike air`; the block is informational only. `model_groups` counts distinct `external_id` and keeps models seen >= 2 times, sorted by count x max vpd; shown in the final summary (whole run) and in `/top` (whole N-day window, before dedup by group).
- G3: settings `CNY_RATE=12.2`, `CARGO_RUB_PER_KG=500`, `CARGO_AIR_RUB_PER_KG=3000`; weight presets `WEIGHT_PRESETS` (by regex over title + category name, first match wins, default 3 kg). Each find line shows `#<find_id>`; `/price <id> <yuan>` stores `Find.china_price` and replies with cost (yuan x rate + kg x ground rate), sale price (the find's cheapest price), margin and its percent of sale, plus the air margin. `/top` adds a margin line under finds with a known China price.

## ADR-010: Recheck of past finds, «ушло за N дней» (2026-10-03)
- Status: accepted (orchestrator; the user delegated decisions). MARKERS NEED LIVE CONFIRMATION.
- Decision: at the start of `/report`, before discovery and crawling, `MarketCrawler._recheck_finds` opens up to `RECHECK_MAX` (default 20, 0 = off) finds that are 2-14 days old and not gone yet, oldest-checked first, one open per `external_id` (a listing found in several runs is opened once, all its rows are updated). Opens go through `_fetch`/`_before_load`, so they count against the run budget and follow the ADR-005 block path; the loop stops early when the budget is reached. Other errors skip the find.
- New columns (idempotent ALTER): `finds.gone_at`, `finds.last_checked_at`, `finds.views_last` (views at the last recheck; `views` keeps the original value).
- Gone detection (`avito_parser.is_gone`): HTTP 404/410 (`Page.status`, new field), or no views counter and a `TEXT_PATTERNS["gone"]` marker in the title/first 3000 chars of visible text («Объявление снято с публикации», «Объявление закрыто», «Товар продан», «Объявление больше не доступно», «Страница не найдена»). «Товар зарезервирован» is not gone (fixtures `market_item*.html` verified). There is no live fixture of a removed page: the marker words are from memory of Avito's UI and must be confirmed on a real removed listing; a missed marker just means the find is rechecked again later.
- Output: the summary gets «✅ Ушло: N (за ~X дн)» with title link, days to gone, vpd and price; `format_find` adds «✅ ушло за X дн» (so `/top` shows it); the model block shows «✅ ушло K» for models with gone listings. Gone finds stay in the DB and in the model aggregation.

## ADR-011: Readable report, cards grouped by category with a reason (2026-10-04)
- Status: accepted (user request: by category, link, short title, views, date, short WHY)
- Portions, final summary and `/top` use one renderer: `Entry` -> `group_entries` (groups by section + subcategory, group order by best vpd, inside a group by vpd) -> `render_groups`. Header `<emoji> <SECTION NAME> — <subcategory>` (`SECTION_EMOJI`, default 📦) is glued to the first card of the group.
- `format_card` replaces the one-line `format_find`: line 1 title link (<= 60 chars, `×N` copies), 2 price / views (+today, total) / date (seller or page date; «дата не проверена»), 3 `💡 reason()` (1-3 clauses by priority: gone, demand, copies, model seen N times, fresh, brand/model for goofish, expensive check), optional `💱` margin, last line goofish link and `#id`.
- `split_message` is block-aware: blocks (cards) are separated by an empty line and are never cut; only a block longer than the limit falls back to line splitting (then to `", "`). `format_summary` joins blocks with empty lines.
- 👍/👎 buttons: the numbers on the cards follow the displayed (grouped) order; one row per find, still only for portions of <= 8 finds.
- Removed: `sort_finds` (🔥/new-first order is replaced by vpd order inside groups), its test, and the «уже было» ordering assertion; «уже было dd.mm» now sits on the date line of the card.

## ADR-012: Live progress message (2026-10-04)
- One message, edited in place: header with the elapsed time since this `/report` press, a 10-char bar by loads/budget, ETA = elapsed / loads x (budget - loads) (shown from 10 loads), «Сейчас» (current section and subcategory, «перепроверка находок», «🧩 жду проверку капчи»), counters with hot finds. A background ticker edits it every `PROGRESS_EDIT_SECONDS` (default 20, 0 = only after each subcategory); an edit is skipped when the text is unchanged. On finish the header is replaced (✅ with time / ⏹ / ⚠️). Pure formatter `format_progress`.


## ADR-013: Results format D, hot finds sent immediately (2026-10-04)
- Status: accepted (user picked design D). Supersedes the per-section portions and the numbered 👍/👎 keyboards of ADR-007/ADR-011.
- During `/report` the only recurring message is the live progress message. Each 🔥 find is sent right after its subcategory is saved, as one `format_card` message with a 👍/👎 row (`fb:<id>:±1`); `Find.sent` now means "that card was sent". Non-hot finds appear only in the final summary.
- Final summary and `/top` share `format_results`: `<b>📊 Проверка рынка · DD.MM</b>` (`📊 Топ находок · N дн` for `/top`), stats line (`🎯 N находок · 🔥 K · 📂 M подкатегорий`), status note, `✅ ушло: X`, then per section `<b><emoji> <Name> — <count></b>` + `<blockquote expandable>` with one `format_line` per find (sorted by vpd, sections by best vpd), a models block in its own expandable quote, and a compact tail (covered days, errors, remaining). The summary lists all finds of the run (hot ones too, marked 🔥) plus finds that went away at recheck (with ✅). `/top` shows margin in the line when the China price is known (`💱 ~M ₽ (N%)`).
- Splitting: blocks are packed into messages <= 4096 chars; a section that does not fit is split into several quotes headed «(продолжение)», never inside a tag.
- Removed: `_portion`, `format_summary`, `format_gone`, `format_models`, `group_entries`, `render_groups`, numbered buttons.

## ADR-014: Market slice per subcategory, dedupe, unified panel style (2026-10-04)
- Status: accepted (user feedback: too little info, unclear «покрыто не полностью», one style for the whole bot)
- Market slice: `crawl_subcategory` stores on `Category` (idempotent ALTERs) `last_fresh_count`, `last_price_median`, `last_opened`, `last_vpd_min/median/max`, `last_best_url/title`. «Fresh» = cards that `_collect` accepted (not promoted, within `REPORT_MAX_AGE_DAYS`, price >= `MIN_PRICE`; Avito already filters `pmin`). VPD stats come from every opened card page, also below `VPD_MIN`; the best card is the best opened one. The summary (and `/top`, for subcategories crawled in the window) has an expandable block «📈 Рынок по подкатегориям — N» with every crawled subcategory sorted by max vpd: `name — 250 свежих за 1.7 дн · ~45 000 ₽ · 👁 8–41/д (медиана 15) · лучшее` («пусто» when no fresh cards, «страницы не открывали» when none opened).
- Dedupe: `dedupe_finds` (by `external_id`, best vpd) in the summary and `/top`; `_save` skips a second row for the same `external_id` in the same run and returns only the rows really added (so hot cards are not sent twice).
- Coverage: «⚠️ Не вся неделя (лимит N стр.): name — 1.7 дн из 7, …»; the days also sit inside the market line.
- Style: `icon()` (normalizes the variation selector, full UnigramIcons set added to `PREMIUM_ICONS`; the existing `📖` key still maps to the clock icon used by the progress panel). New `app/services/panels.py` holds the panel-style texts (`/start`, `/status`, startup, captcha and block alerts, `/price`); `/cats`, summary head/stats, hot cards, `/top` use the same look (plain labels, numbers in `<code>`, icons only when `PREMIUM_EMOJI=true`, plain emoji otherwise). Scanner got a `premium` argument. `tests/conftest.py` forces `settings.premium_emoji=False` so a developer `.env` does not change test output.

## ADR-015: Market history foundation (Radar V2, Phase 0–1) (2026-10-03)
- Status: accepted (user: «начинай»). Design: `docs/radar-v2-design.md`, research: `docs/deep-research.md`.
- Phase 0 check — **item id is not a creation-time watermark**: on 35 finds with a real date (seller/page) the Spearman correlation of id and date is 0.02 (Avito sorts and dates by last activation/raise). The «id watermark» incremental stop from the design is dropped; the sweep (Phase 2) stops by the share of already-known non-promo ids on a fully read page plus the 7-day and depth limits.
- History = one row per listing + change events, not a snapshot per sighting. New tables (created by `create_all`, no ALTER): `ads` (id = avito item id, category, title, `model_key`, price, `url_path` without query, city, shop name when the search card has it, seller url from the card, image url, best known `posted_at` with source search < card < seller, `first_seen_at`/`last_seen_at`, `promoted_seen`, `status` live|gone), `ad_events` (price/title/status changes), `card_obs` (views/today per card open; bucket report|recheck|backfill), `scan_categories` (per run × category: pages, non-promo cards seen, new vs known, `total_count`, covered hours, stop reason). `crawl_runs` plays the role of the design's `scan` table.
- `/report` behaviour is unchanged; it now records: every search page right after it is read (own commit, so a block or stop mid-category keeps the pages), every opened card (`card_obs`, date and seller url refine the ad), the seller-profile date, recheck views (`recheck`) and gone status (event). Status «gone» still relies on unverified markers (ADR-010) — it is not «sold».
- New parser field `parse_total_count` (`page-title/count`, «17 888»): free supply series per category, read from page 1.
- Fix: an empty first search page while the header says N > 0 listings is a layout failure, not «the whole week covered» (covered = 0, stop reason `empty`). An empty page without a counter keeps the old meaning.
- `scripts/backfill_history.py` moves existing finds into `ads` + `card_obs` (idempotent via bucket `backfill`). Run on 2026-10-03: 34 listings; DB backup `data/app.db.bak-20261003-phase1`.

## ADR-016: Daily incremental sweep of all categories (Radar V2, Phase 2) (2026-10-04)
- Status: accepted (user: «дальше идем»).
- Avito «Аналитика спроса» is **not** used: it needs a paid tariff (~8 000 ₽). Demand stays our own measurement (card views, baseline per category — Phase 4).
- `/sweep` and a daily job read only search pages of every non-skipped category — no cards, no seller profiles. A run is `crawl_runs.kind = 'sweep'` with its own budget `SWEEP_BUDGET` (800); `/report` runs are `kind = 'report'` and keep `REPORT_BUDGET`. A run resumes only a run of the same kind (12 h window, as before).
- Depth per category: `rate` = median of `cards_seen / window_hours` over its last 7 finished scans (any kind; windows < 1 h ignored); `pages = ceil(rate × hours since last scan / 50 × 1.3) + 1`, clamped to [2, `SWEEP_MAX_PAGES`=10]; no history → `SWEEP_FIRST_PAGES`=3. Categories with < `SWEEP_QUIET_PER_DAY` (25) new listings/day scanned less than 36 h ago are skipped (every other day). Order: longest since last scan first.
- Stops (same `_collect` as `/report`): 7-day age limit, empty page, depth cap, and for the sweep `known` — a fully read page where ≥ `SWEEP_KNOWN_STOP` (0.85) of non-promo listings were already in `ads`. No id watermark (ADR-015).
- Checkpoint: after every page `scan_categories` gets a `done = false` row (`stop_reason = partial`) in the same commit as the listings; a resumed sweep continues that category from the next page with the counters kept. `/report` still restarts an interrupted subcategory from page 1.
- Schedule: APScheduler cron `DAILY_SWEEP_AT` (09:00 Europe/Moscow, misfire grace 6 h, max_instances 1) plus a one-off catch-up 2 min after bot start; both call `MarketCrawler.daily()`, which starts/resumes a sweep only if the time has passed today, nothing is running and today has no `done`/`budget` sweep (a `blocked`/`stopped` one is resumed). `DAILY_SWEEP_AT=""` disables the schedule.
- Telemetry: progress message «📡 Обход рынка» (new listings instead of finds); final message with categories done / total, quiet skipped, pages, loads, new vs known listings, stop reasons, captcha waits, errors, and «не досмотрено» for depth-capped categories with covered hours.
- Data: the 209 regular categories were un-skipped (brand mode is over); all 257 categories are active. `/skip` turns any back off.
