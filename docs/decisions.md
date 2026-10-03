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

## ADR-004: Live verification spike (I1) — BLOCKED, answers pending (2026-10-03)
- Status: incomplete. `scripts/save_fixtures.py crawl` (provider launch args, headed, persistent profile, no bot running) got HTTP 439 / «Доступ ограничен: проверка безопасности» on the very FIRST load (warm-up `https://www.avito.ru`). Per safety rule: no solving, no retry, no `app.auth`, browser closed. Loads used: 1 of 25.
- (a) seller profile shows listing dates: NOT VERIFIED. (b) card-page date changes on raise: NOT VERIFIED → `CHECK_SELLER_DATE` stays true. (c) date formats / promo marker / item-date selectors / views regex: NOT VERIFIED (design relies on catalog.js: «№ id · дата · N просмотров (+M сегодня)», promo = «Продвинуто|Забронировано»; selectors vs existing parser: no diff observed).
- Likely cause: IP/profile flagged by Avito after the earlier monitor runs; the block page appeared on the home page, before any new traffic pattern from this spike. Needs a human decision (wait for cooldown, proxy, or re-auth by the user via `app.auth`).
- (f) Decision recorded: `crawl_order` is section-grouped as designed in tech-design §6 (plan N5/m5 accepted).
- Rerun: after the block clears, `PYTHONIOENCODING=utf-8 .venv/Scripts/python -m scripts.save_fixtures crawl <section_slug>` then `sanitize RAW_NAME FIXTURE_NAME` per fixture.

## ADR-005: Captcha → human solves it, crawl pauses (2026-10-03)
- Status: accepted (explicit user request)
- Context: spike I1 hit «Доступ ограничен: проверка безопасности» (HTTP 439) on warm-up; user solved the captcha manually in the bot's browser window (`python -m app.auth`) and asked for this flow.
- Decision: bot never solves/bypasses captcha. On captcha/block page during /report: send Telegram alert «🧩 Avito просит капчу — пройди её в окне браузера бота», pause the crawl (browser stays open, HEADLESS=false), poll the page every ~30 s for up to CAPTCHA_WAIT_MINUTES (default 30); when the block page is gone → continue; on timeout → stop as before (save state, send collected finds). Supersedes the "stop immediately on block" part of AC-5.x for /report. Monitor (D&G) behaviour unchanged (alert + skip).
- Plan impact: implement in I11 (block handling); requirements AC-5.1 to be updated accordingly.
- Retry after the user solved the captcha manually (2026-10-03): first load (home warm-up) returned HTTP 429, title «Доступ ограничен: проблема с IP». Stopped immediately, no retry, browser closed, loads used 1/25 (2 across both attempts). The block is IP-level, not profile-level: a solved captcha does not help. Options for the human: wait for cooldown, change IP/proxy (`AVITO_PROXY`), then rerun the script. (a)(b)(c) remain unverified.
