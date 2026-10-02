from app.config import playwright_proxy


def test_playwright_proxy():
    assert playwright_proxy("") is None
    assert playwright_proxy("http://1.2.3.4:8000") == {"server": "http://1.2.3.4:8000"}
    assert playwright_proxy("http://us%40r:p%3Ass@proxy.ru:3128") == {
        "server": "http://proxy.ru:3128",
        "username": "us@r",
        "password": "p:ss",
    }
