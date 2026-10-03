## Security Review: avito-hunter, diff 08a809f..HEAD (app, scripts, tests, .env.example) + overall app/

Scope: single-admin Telegram bot + Playwright crawler on a local Windows PC. Static review only; app not run, no network, .env values not read.
Checklist adapted from skills/security-reviewer.md to Python (aiogram, SQLAlchemy, Playwright, BeautifulSoup).

### CRITICAL
- none

### HIGH
- none

### MEDIUM
- `app/services/scanner.py:92` and `app/services/market.py:303` — Text sent with the global parse_mode=HTML is built from unescaped dynamic data: `ProviderBlocked` message contains the scraped page title (`avito_browser.py:96`), and `section.replace('_',' ')` is a slug scraped from hrefs. A hostile or odd title (`<`, `&`) makes Telegram reject the alert (BadRequest, swallowed in `send_text`) so the block alert is lost; only Telegram-whitelisted tags can be injected (no script risk). Fix: `html.escape(str(e))` and `html.escape(section)`; same for `report.append(f"... {e}")` which is escaped later in `/check` (OK) but not in the scanner alert.
- `app/providers/avito_parser.py:60-62` — `normalize_url` keeps the host of any absolute href (`urlunsplit(("https", parts.netloc...))`). A card or seller link pointing off-domain is stored, shown as a link in Telegram (`notifier.py:41`, `handlers.py:137`) and later opened by the crawler browser (`market.py` fetches `card.url`/`seller_url` using the logged-in persistent profile). Fix: reject/skip hrefs whose netloc is not `www.avito.ru` / `avito.ru`.

### LOW
- `tests/fixtures/market_seller.html`, `market_seller2.html` — sanitize() masked seller names but third-party buyer first names and review text remain in review blocks (e.g. "Татьяна", "Покупатель" reviews with item titles). Low personal-data exposure (first names, public reviews); repo is local, no remote. Fix: extend sanitize() to mask `review(*)/header/title`, or re-sanitize before any publishing. Phone-like digit strings found in fixtures are item ids, not phones; no emails or tokens found. `avito_blocked.html` contains only Avito's own public captcha JS.
- `app/telegram/handlers.py:21` — Auth is `router.message.filter(F.chat.id == admin_chat_id)`: correct and silent for strangers; applies to messages only (no callbacks/inline used, so no bypass). Caveat: if the admin chat id is a group, every member is admin. Keep it a private chat; optionally also filter `F.from_user.id`. Fail-closed at startup (`main.py:44` exits if token/chat id empty).
- `requirements.txt` — Lower bounds only for playwright, aiohttp-socks, pydantic-settings, beautifulsoup4 and no lock file/hashes; floating versions risk supply-chain surprises. Fix: `pip freeze`/pip-compile lock for the runtime deps. Dev deps (pytest, ruff) share the runtime file.
- `scripts/import_map.py:44-46` — Path comes from argv/hardcoded default and is only read as CSV (no write, no traversal risk for a local operator tool). Robustness: `r["key"].split("/")[2]` and `float(r["score"])` raise on malformed rows; no key validation (should match `^/rossiya/[a-z0-9_]+/`). Values go through SQLAlchemy ORM, so no injection.
- `app/db.py:120` — Only raw SQL is a constant `ALTER TABLE`; all other queries use the SQLAlchemy ORM/expressions with bound params (`handlers.py`, `market.py`). No injection found. No f-string SQL anywhere (grep-verified).
- `app/providers/avito_browser.py:122-126` — Blocked/empty page HTML is dumped to `data/debug/` (may contain account-specific content/cookies-in-DOM); `data/` is gitignored, so only local exposure. Consider cleaning old dumps. `scripts/save_fixtures.py:40` builds a Telegram URL with the bot token but only inside `urlopen` with errors suppressed (not logged).
- `app/services/scanner.py:100`, `market.py:428,461` — Exception text (`{e}`) goes into logs and Telegram; Playwright/proxy errors may echo the proxy host (no credentials seen in messages by design, but `AVITO_PROXY` has user:pass in .env, so never log `settings.avito_proxy`; currently not logged).

### Verified OK
- Secrets: `.env`, `data/` (db, browser profile, logs, debug) gitignored and not tracked (`git ls-files`); `.env.example` has empty token/chat id/proxy, only placeholders. No hardcoded secrets in app/ or scripts/.
- HTML escaping: titles, locations, categories, seller names, rule names, keywords, URLs (`html.escape` with quote=True inside href) are escaped in `notifier.py`, `handlers.py`, `market_logic.py` (formatters, errors, covered categories).
- No `eval/exec/pickle/subprocess/os.system`; no shell built from input; BeautifulSoup used on DOM only.
- Logging: log lines include listing ids, truncated titles, urls; no tokens/proxy creds/phone numbers logged.
- Browser: no stealth/captcha bypass; human passes captcha (ADR-005). Profile is a local Chromium profile (contains Avito session cookies; protect `data/avito_profile` as a credential).

---
Verdict: APPROVED (no CRITICAL/HIGH; 2 MEDIUM recommended before further crawling expansion, 6 LOW). No human gate required (no HIGH/CRITICAL).
