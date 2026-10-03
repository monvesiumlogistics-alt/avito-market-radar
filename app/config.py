from urllib.parse import unquote, urlsplit

from pydantic_settings import BaseSettings, SettingsConfigDict

# 25 разделов TOP из avito_niche/catalog.js
TOP_SECTIONS = (
    "telefony,audio_i_video,tovary_dlya_kompyutera,noutbuki,nastolnye_kompyutery,planshety_i_elektronnye_knigi,"
    "orgtehnika_i_rashodniki,fototehnika,igry_pristavki_i_programmy,bytovaya_tehnika,odezhda_obuv_aksessuary,"
    "detskaya_odezhda_i_obuv,tovary_dlya_detey_i_igrushki,chasy_i_ukrasheniya,krasota_i_zdorove,"
    "remont_i_stroitelstvo,mebel_i_interer,posuda_i_tovary_dlya_kuhni,kollektsionirovanie,muzykalnye_instrumenty,"
    "ohota_i_rybalka,sport_i_otdyh,velosipedy,tovary_dlya_zhivotnyh,zapchasti_i_aksessuary"
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    telegram_bot_token: str = ""
    telegram_admin_chat_id: int = 0
    telegram_proxy: str = ""

    check_interval_minutes: int = 60
    initial_scan_notify: bool = False

    avito_search_urls: str = ""
    avito_profile_path: str = "./data/avito_profile"
    avito_proxy: str = ""  # http://user:pass@host:port — только для браузера Avito
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

    # Проверка рынка (/report)
    report_sections: str = TOP_SECTIONS  # slug'и через запятую; в .env только переопределение
    report_max_subcats: int = 18
    report_max_pages: int = 5
    report_max_age_days: int = 7
    min_price: int = 10000
    vpd_min: int = 50  # просмотров в день: ниже — не находка
    vpd_hot: int = 100  # от этого — 🔥
    report_cards_per_subcat: int = 12
    report_budget: int = 600  # загрузок страниц на прогон
    report_resume_hours: int = 12
    # сколько ждать, пока человек пройдёт капчу в окне бота (нужен HEADLESS=false); 0 = не ждать
    captcha_wait_minutes: int = 15
    check_seller_date: bool = True
    premium_emoji: bool = False  # иконки UnigramIcons (custom emoji) в прогрессе; без них — обычные эмодзи
    progress_edit_seconds: int = 20  # как часто правится сообщение о прогрессе; 0 = только после подкатегории
    recheck_max: int = 20  # сколько прошлых находок (2-14 дней) перепроверять в начале /report; 0 = выключено
    # маржа (/price): курс юаня и тарифы доставки Китай -> Москва, ₽ за кг
    cny_rate: float = 12.2
    cargo_rub_per_kg: float = 500  # наземка
    cargo_air_rub_per_kg: float = 3000  # авиа
    # Ежедневный обход выдачи (ADR-016): только страницы выдачи, без карточек
    daily_sweep_at: str = "09:00"  # МСК; "" — без расписания (только /sweep)
    sweep_budget: int = 800  # загрузок на обход
    sweep_first_pages: int = 3  # страниц для категории без истории
    sweep_max_pages: int = 10
    sweep_known_stop: float = 0.85  # стоп, когда такая доля непромо-объявлений страницы уже знакома
    sweep_quiet_per_day: int = 25  # тише — категория обходится через день
    page_delay_min: float = 2
    page_delay_max: float = 5

    database_url: str = "sqlite:///./data/app.db"
    log_level: str = "INFO"


def playwright_proxy(url: str) -> dict | None:
    """'http://user:pass@host:port' -> формат proxy для Playwright."""
    if not url:
        return None
    parts = urlsplit(url)
    proxy = {"server": f"{parts.scheme}://{parts.hostname}:{parts.port}"}
    if parts.username:
        proxy |= {"username": unquote(parts.username), "password": unquote(parts.password or "")}
    return proxy


def split_csv(raw: str) -> list[str]:
    return [part.strip() for part in raw.split(",") if part.strip()]


settings = Settings()
