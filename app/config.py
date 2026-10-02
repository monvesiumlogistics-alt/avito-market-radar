from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str = ""
    telegram_admin_chat_id: int = 0
    telegram_proxy: str = ""

    check_interval_minutes: int = 60
    initial_scan_notify: bool = False

    avito_search_urls: str = ""
    avito_profile_path: str = "./data/avito_profile"
    headless: bool = False
    max_pages: int = 3

    rule_name: str = "Dolce & Gabbana <= 2000"
    keywords: str = (
        "dolce gabbana,dolce & gabbana,dolce&gabbana,dolce gabana,dolce gabanna,"
        "дольче габбана,дольче и габбана,дольче габана"
    )
    exclude_keywords: str = (
        "реплика,копия,люкс,lux,premium,aaa,1:1,под оригинал,как оригинал,качество оригинала,replica"
    )
    price_min: int | None = 0
    price_max: int | None = 2000

    database_url: str = "sqlite:///./data/app.db"
    log_level: str = "INFO"


def split_csv(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


settings = Settings()
