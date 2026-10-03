from datetime import datetime
from pathlib import Path

from sqlalchemy import JSON, ForeignKey, String, Text, UniqueConstraint, create_engine, inspect, select, text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker

from app.config import Settings, split_csv
from app.models import parse_search_urls


class Base(DeclarativeBase):
    pass


class WatchRule(Base):
    __tablename__ = "watch_rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_chat_id: Mapped[int | None]  # задел под мультиюзер; сейчас один админ
    name: Mapped[str] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(default=True)
    keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    exclude_keywords: Mapped[list[str]] = mapped_column(JSON, default=list)
    price_min: Mapped[int | None]
    price_max: Mapped[int | None]
    search_urls: Mapped[list[dict]] = mapped_column(JSON, default=list)  # [{"label": ..., "url": ...}]
    check_interval_minutes: Mapped[int | None]  # None = глобальный CHECK_INTERVAL_MINUTES
    last_checked_at: Mapped[datetime | None]  # None = следующий скан первый (молчаливая индексация)


class ListingRow(Base):
    __tablename__ = "listings"
    __table_args__ = (UniqueConstraint("rule_id", "source", "external_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    rule_id: Mapped[int] = mapped_column(ForeignKey("watch_rules.id"), index=True)
    source: Mapped[str] = mapped_column(String(32))
    external_id: Mapped[str] = mapped_column(String(64))
    title: Mapped[str] = mapped_column(String(500))
    description: Mapped[str | None] = mapped_column(Text)
    price: Mapped[int | None]
    currency: Mapped[str] = mapped_column(String(8), default="RUB")
    url: Mapped[str] = mapped_column(String(1000))
    image_url: Mapped[str | None] = mapped_column(String(1000))
    location: Mapped[str | None] = mapped_column(String(300))
    seller_name: Mapped[str | None] = mapped_column(String(300))
    category: Mapped[str | None] = mapped_column(String(200))
    published_at: Mapped[datetime | None]
    first_seen_at: Mapped[datetime]
    last_seen_at: Mapped[datetime]
    matched: Mapped[bool]
    # None: не подошло; skipped: проиндексировано первым сканом; pending: ждёт отправки; sent: отправлено
    notify_status: Mapped[str | None] = mapped_column(String(16), index=True)
    notified_at: Mapped[datetime | None]


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True)
    section: Mapped[str] = mapped_column(String(64))
    name: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(300), unique=True)
    discovered_at: Mapped[datetime]
    last_crawled_at: Mapped[datetime | None]
    last_run_id: Mapped[int | None]  # «пройдена в этом прогоне» = last_run_id == run.id
    last_best_vpd: Mapped[int | None]
    last_status: Mapped[str | None] = mapped_column(String(16))  # ok | error
    last_days_covered: Mapped[float | None]
    prior_score: Mapped[float | None]  # стартовый приоритет из карты (catalog.csv), пока не обходили
    skipped: Mapped[bool] = mapped_column(default=False)  # /skip: категорию не обходим
    # срез рынка по последнему обходу (ADR-014)
    last_fresh_count: Mapped[int | None]
    last_price_median: Mapped[int | None]
    last_opened: Mapped[int | None]
    last_vpd_min: Mapped[int | None]
    last_vpd_median: Mapped[int | None]
    last_vpd_max: Mapped[int | None]
    last_best_url: Mapped[str | None] = mapped_column(String(1000))
    last_best_title: Mapped[str | None] = mapped_column(String(200))


class CrawlRun(Base):
    __tablename__ = "crawl_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    started_at: Mapped[datetime]
    finished_at: Mapped[datetime | None]
    # running | stopped | blocked | interrupted | budget | done | failed
    status: Mapped[str] = mapped_column(String(16), default="running")
    kind: Mapped[str] = mapped_column(String(16), default="report")  # report (/report) | sweep (обход выдачи)
    loads: Mapped[int] = mapped_column(default=0)
    finds_count: Mapped[int] = mapped_column(default=0)
    progress_msg_id: Mapped[int | None]


class Find(Base):
    __tablename__ = "finds"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("crawl_runs.id"))
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    group_key: Mapped[str] = mapped_column(String(300), index=True)
    title: Mapped[str] = mapped_column(String(500))
    price_min: Mapped[int]
    price_max: Mapped[int]
    views: Mapped[int | None]  # всего просмотров на карточке
    vpd: Mapped[int]
    today: Mapped[int | None]
    page_date: Mapped[datetime | None]  # дата со страницы карточки
    seller_date: Mapped[datetime | None]  # дата из профиля продавца, если нашлась
    age_days: Mapped[float]
    date_checked: Mapped[bool]
    copies: Mapped[int]
    url: Mapped[str] = mapped_column(String(1000))
    external_id: Mapped[str] = mapped_column(String(64))
    hot: Mapped[bool]
    sent: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime]
    china_price: Mapped[int | None]  # US-8, в v1 пусто
    gone_at: Mapped[datetime | None]  # объявление исчезло при перепроверке: «ушло» = спрос
    last_checked_at: Mapped[datetime | None]
    views_last: Mapped[int | None]  # просмотры на момент последней перепроверки
    feedback: Mapped[int] = mapped_column(default=0)  # 👍 +1 / 👎 -1 из кнопок под порцией


# --- история рынка (ADR-015): одна строка на объявление + изменения, а не снимок на каждую встречу ---


class Ad(Base):
    """Каждое объявление, увиденное в выдаче. Повторная встреча обновляет last_seen_at; изменения — в ad_events."""

    __tablename__ = "ads"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # avito item id
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"))
    title: Mapped[str] = mapped_column(String(500))
    model_key: Mapped[str | None] = mapped_column(String(200), index=True)
    price: Mapped[int | None]
    url_path: Mapped[str] = mapped_column(String(500))  # без ?context=...
    city: Mapped[str | None] = mapped_column(String(300))
    shop: Mapped[str | None] = mapped_column(String(300))  # имя продавца из выдачи (есть у магазинов, у частников нет)
    seller_url: Mapped[str | None] = mapped_column(String(500))  # из карточки
    image_url: Mapped[str | None] = mapped_column(String(1000))
    posted_at: Mapped[datetime | None]  # лучшая известная дата публикации
    posted_src: Mapped[str | None] = mapped_column(String(8))  # search | card | seller (по возрастанию точности)
    first_seen_at: Mapped[datetime] = mapped_column(index=True)
    last_seen_at: Mapped[datetime]
    promoted_seen: Mapped[int] = mapped_column(default=0)
    status: Mapped[str] = mapped_column(String(16), default="live")  # live | gone (маркеры снятия: ADR-010)
    status_at: Mapped[datetime | None]


class AdEvent(Base):
    __tablename__ = "ad_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    ad_id: Mapped[str] = mapped_column(String(64), index=True)
    at: Mapped[datetime]
    kind: Mapped[str] = mapped_column(String(16))  # price | title | status
    old: Mapped[str | None] = mapped_column(String(500))
    new: Mapped[str | None] = mapped_column(String(500))


class CardObs(Base):
    """Замер страницы объявления. Несколько замеров одного объявления дают текущую скорость просмотров."""

    __tablename__ = "card_obs"

    id: Mapped[int] = mapped_column(primary_key=True)
    ad_id: Mapped[str] = mapped_column(String(64), index=True)
    at: Mapped[datetime]
    run_id: Mapped[int | None]
    views: Mapped[int | None]
    today: Mapped[int | None]
    bucket: Mapped[str] = mapped_column(String(16))  # report | recheck | backfill (дальше — корзины сэмплера)


class ScanCategory(Base):
    """Итог выдачи одной категории за прогон: покрытие, новизна, общее число объявлений «на полке»."""

    __tablename__ = "scan_categories"

    run_id: Mapped[int] = mapped_column(ForeignKey("crawl_runs.id"), primary_key=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id"), primary_key=True)
    at: Mapped[datetime]
    pages: Mapped[int]
    cards_seen: Mapped[int]  # непромо-карточек на прочитанных страницах
    new_ads: Mapped[int]  # из них впервые увиденных
    known_ads: Mapped[int]
    total_count: Mapped[int | None]  # «17 888» в заголовке выдачи (с фильтром pmin)
    window_hours: Mapped[float]  # какой промежуток времени покрыт прочитанными страницами
    stop_reason: Mapped[str] = mapped_column(String(16))  # known | age_limit | depth_cap | empty | partial
    done: Mapped[bool] = mapped_column(default=True)  # False — прервана: чекпойнт, обход продолжит со следующей стр.


_ADDED_COLUMNS = (  # (таблица, колонка, тип) — константы, не ввод пользователя
    ("categories", "prior_score", "FLOAT"),
    ("categories", "skipped", "BOOLEAN NOT NULL DEFAULT 0"),
    ("finds", "feedback", "INTEGER NOT NULL DEFAULT 0"),
    ("categories", "last_fresh_count", "INTEGER"),
    ("categories", "last_price_median", "INTEGER"),
    ("categories", "last_opened", "INTEGER"),
    ("categories", "last_vpd_min", "INTEGER"),
    ("categories", "last_vpd_median", "INTEGER"),
    ("categories", "last_vpd_max", "INTEGER"),
    ("categories", "last_best_url", "VARCHAR(1000)"),
    ("categories", "last_best_title", "VARCHAR(200)"),
    ("finds", "gone_at", "DATETIME"),
    ("finds", "last_checked_at", "DATETIME"),
    ("finds", "views_last", "INTEGER"),
    ("crawl_runs", "kind", "VARCHAR(16) NOT NULL DEFAULT 'report'"),
    ("scan_categories", "done", "BOOLEAN NOT NULL DEFAULT 1"),
)


def init_db(url: str) -> sessionmaker:
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url)
    Base.metadata.create_all(engine)  # ponytail: create_all без миграций, Alembic при переходе на PostgreSQL
    for table, column, ddl in _ADDED_COLUMNS:  # create_all не меняет существующие таблицы
        if column not in {c["name"] for c in inspect(engine).get_columns(table)}:
            with engine.begin() as conn:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}"))
    return sessionmaker(engine, expire_on_commit=False)


def sync_default_rule(session_factory: sessionmaker, s: Settings) -> WatchRule:
    """Правило №1 живёт в .env: при каждом старте синхронизируем его с конфигом."""
    with session_factory() as db:
        rule = db.scalar(select(WatchRule).order_by(WatchRule.id).limit(1))
        if rule is None:
            rule = WatchRule(name=s.rule_name)
            db.add(rule)
        urls = [{"label": u.label, "url": u.url} for u in parse_search_urls(s.avito_search_urls)]
        if rule.search_urls != urls:
            rule.last_checked_at = None  # новые URL сначала индексируем молча, без спама
        rule.name = s.rule_name
        rule.keywords = split_csv(s.keywords)
        rule.exclude_keywords = split_csv(s.exclude_keywords)
        rule.price_min = s.price_min
        rule.price_max = s.price_max
        rule.search_urls = urls
        db.commit()
        return rule
