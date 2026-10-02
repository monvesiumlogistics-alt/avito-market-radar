from datetime import datetime
from pathlib import Path

from sqlalchemy import JSON, ForeignKey, String, Text, UniqueConstraint, create_engine, select
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


def init_db(url: str) -> sessionmaker:
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(url)
    Base.metadata.create_all(engine)  # ponytail: create_all без миграций, Alembic при переходе на PostgreSQL
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
