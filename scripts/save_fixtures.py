"""Re-capture live Avito pages as test fixtures (run when markup changes). Needs the bot stopped (profile lock).

    PYTHONIOENCODING=utf-8 .venv/Scripts/python -m scripts.save_fixtures crawl [section_slug]
    PYTHONIOENCODING=utf-8 .venv/Scripts/python -m scripts.save_fixtures sanitize RAW_NAME FIXTURE_NAME

`crawl` saves RAW html to data/fixtures_raw/ (never commit) and prints a manifest; `sanitize` strips seller
names/phones and writes tests/fixtures/<FIXTURE_NAME>.html. Hard cap MAX_LOADS page loads; stops on any block.
"""

import asyncio
import contextlib
import random
import re
import sys
from pathlib import Path

from app.config import playwright_proxy, settings
from app.providers.avito_browser import AvitoBrowserProvider
from app.providers.avito_parser import BASE_URL, is_blocked
from app.providers.base import ProviderBlocked

MAX_LOADS = 25
RAW = Path("data/fixtures_raw")
FIX = Path("tests/fixtures")
CARDS_TO_OPEN = 6
_BLOCK = ("Вы робот", "Доступ ограничен", "firewall")
loads = 0


async def load(provider: AvitoBrowserProvider, url: str, name: str, ready: str | None = None) -> str:
    global loads
    if loads >= MAX_LOADS:
        raise SystemExit(f"load cap {MAX_LOADS} reached")
    loads += 1
    await asyncio.sleep(random.uniform(2, 5))
    tab = await provider._ctx.new_page()
    try:
        resp = await tab.goto(url, wait_until="domcontentloaded", timeout=45_000)
        if ready:
            with contextlib.suppress(Exception):
                await tab.wait_for_selector(ready, timeout=15_000)
        html, title = await tab.content(), await tab.title()
    finally:
        await tab.close()
    status = resp.status if resp else 0
    if status in (403, 429) or is_blocked(html, title) or any(m in title for m in _BLOCK):
        (RAW / f"BLOCKED_{name}.html").write_text(html, encoding="utf-8")
        raise ProviderBlocked(f"{url} status={status} title={title!r}")
    (RAW / f"{name}.html").write_text(html, encoding="utf-8")
    print(f"[{loads}/{MAX_LOADS}] {status} {name} {len(html)}b {url}")
    return html


async def crawl(slug: str) -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    proxy = playwright_proxy(settings.avito_proxy)
    provider = AvitoBrowserProvider(settings.avito_profile_path, settings.headless, proxy)
    async with provider:
        try:
            await load(provider, BASE_URL, "home")  # warm-up
            sec = await load(provider, f"{BASE_URL}/rossiya/{slug}", "section", '[data-marker="item"]')
            subs = re.findall(rf'href="(/[a-z_]+/{slug}/[a-z0-9_]+)"', sec)
            subs = list(dict.fromkeys(subs))
            print("subcats:", subs)
            if not subs:
                return
            sub = subs[0]
            html = await load(provider, f"{BASE_URL}{sub}?s=104", "search", '[data-marker="item"]')
            ids = list(dict.fromkeys(re.findall(r'href="(/[a-z_]+/[a-z_0-9]+/[^"?]+_(\d{8,}))', html)))
            print("cards on search:", len(ids))
            for href, cid in ids[:CARDS_TO_OPEN]:
                await load(provider, BASE_URL + href, f"item_{cid}")
        except ProviderBlocked as e:
            print("BLOCKED, stopping, no retry:", e)
        finally:
            print("loads used:", loads)


_PHONE = re.compile(r"(?:\+7|8)[\s\-()]*\d{3}[\s\-()]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}")


def sanitize(html: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    sel = 'a[href*="/user/"], a[href*="/brands/"], [data-marker^="seller-info"], [data-marker^="seller-link"]'
    for a in soup.select(sel):
        for t in a.find_all(string=True):
            t.replace_with("SELLER" if t.strip() else t)
    out = _PHONE.sub("+70000000000", str(soup))
    return re.sub(r"[\w.+-]+@[\w-]+\.[\w.]+", "user@example.com", out)


if __name__ == "__main__":
    if sys.argv[1] == "crawl":
        asyncio.run(crawl(sys.argv[2] if len(sys.argv) > 2 else "muzykalnye_instrumenty"))
    elif sys.argv[1] == "sanitize":
        FIX.mkdir(parents=True, exist_ok=True)
        raw = (RAW / f"{sys.argv[2]}.html").read_text(encoding="utf-8")
        (FIX / f"{sys.argv[3]}.html").write_text(sanitize(raw), encoding="utf-8")
