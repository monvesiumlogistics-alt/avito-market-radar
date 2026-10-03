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
