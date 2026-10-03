import pytest

from app.providers.avito_browser import AvitoBrowserProvider, status_blocked
from app.providers.base import ProviderBlocked


def test_status_blocked():
    assert all(status_blocked(s) for s in (403, 429, 439))
    assert not any(status_blocked(s) for s in (200, 404, 500, None))


class Stub:
    def __init__(self):
        self.closed = 0

    async def close(self):
        self.closed += 1

    stop = close


async def test_double_aexit_safe_and_resets_state():
    p = AvitoBrowserProvider("./x", headless=True)
    ctx, pw = Stub(), Stub()
    p._ctx, p._pw, p._warmed = ctx, pw, True
    await p.__aexit__(None, None, None)
    await p.__aexit__(None, None, None)
    assert (ctx.closed, pw.closed) == (1, 1)
    assert p._ctx is None and p._pw is None and not p._warmed


class FakeResp:
    def __init__(self, status):
        self.status = status


class FakeTab:
    url = "https://www.avito.ru/final"

    def __init__(self, statuses, html="<html></html>", title=""):
        self.statuses, self.html, self.title_ = list(statuses), html, title

    async def goto(self, url, **kw):
        return FakeResp(self.statuses.pop(0))

    async def wait_for_timeout(self, ms):
        pass

    async def wait_for_selector(self, sel, **kw):
        pass

    async def content(self):
        return self.html

    async def title(self):
        return self.title_

    async def close(self):
        pass


class FakeCtx:
    def __init__(self, tab):
        self.tab = tab

    async def new_page(self):
        return self.tab


def _provider(tab):
    p = AvitoBrowserProvider("./x", headless=True)
    p._ctx = FakeCtx(tab)
    return p


async def test_fetch_returns_page():
    page = await _provider(FakeTab([200, 200], "<html>ok</html>", "T")).fetch("https://www.avito.ru/a", "x")
    assert (page.html, page.title, page.final_url) == ("<html>ok</html>", "T", "https://www.avito.ru/final")


@pytest.mark.parametrize("status", [403, 429, 439])
async def test_fetch_blocked_status(status):
    with pytest.raises(ProviderBlocked):
        await _provider(FakeTab([200, status])).fetch("https://www.avito.ru/a")


async def test_fetch_blocked_on_warmup_status():
    with pytest.raises(ProviderBlocked):
        await _provider(FakeTab([429])).fetch("https://www.avito.ru/a")


async def test_fetch_blocked_by_page_text():
    with pytest.raises(ProviderBlocked):
        await _provider(FakeTab([200, 200], "<html>Вы робот?</html>")).fetch("https://www.avito.ru/a")
