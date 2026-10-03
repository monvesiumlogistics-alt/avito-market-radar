from app.config import Settings, playwright_proxy, split_csv


def test_playwright_proxy():
    assert playwright_proxy("") is None
    assert playwright_proxy("http://1.2.3.4:8000") == {"server": "http://1.2.3.4:8000"}
    assert playwright_proxy("http://us%40r:p%3Ass@proxy.ru:3128") == {
        "server": "http://proxy.ru:3128",
        "username": "us@r",
        "password": "p:ss",
    }


def test_report_defaults_and_env_override(monkeypatch):
    s = Settings(_env_file=None)
    assert len(split_csv(s.report_sections)) == 25 and "telefony" in s.report_sections
    assert (s.report_budget, s.min_price, s.vpd_min, s.vpd_hot, s.check_seller_date) == (600, 10000, 50, 100, True)
    monkeypatch.setenv("REPORT_SECTIONS", "telefony,noutbuki")
    monkeypatch.setenv("CHECK_SELLER_DATE", "false")
    s = Settings(_env_file=None)
    assert split_csv(s.report_sections) == ["telefony", "noutbuki"] and s.check_seller_date is False
