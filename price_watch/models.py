from datetime import UTC, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Table,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def utcnow() -> datetime:
    return datetime.now(UTC)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(64), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))


class Store(Base):
    __tablename__ = "stores"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    domain: Mapped[str] = mapped_column(String(255), unique=True)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=60)
    # "http" = httpx + BeautifulSoup; "browser" = Playwright (páginas com JS)
    read_mode: Mapped[str] = mapped_column(String(16), default="http")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


rule_groups = Table(
    "rule_groups",
    Base.metadata,
    Column("rule_id", ForeignKey("rules.id", ondelete="CASCADE"), primary_key=True),
    Column("group_id", ForeignKey("tg_groups.id", ondelete="CASCADE"), primary_key=True),
)


class TgGroup(Base):
    __tablename__ = "tg_groups"

    id: Mapped[int] = mapped_column(primary_key=True)
    # id "marcado" do Telethon (ex.: -100123...), igual a event.chat_id
    chat_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    title: Mapped[str] = mapped_column(String(255))
    username: Mapped[str | None] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))  # group | supergroup | channel
    monitored: Mapped[bool] = mapped_column(Boolean, default=False)


class Rule(Base):
    __tablename__ = "rules"

    id: Mapped[int] = mapped_column(primary_key=True)
    keyword: Mapped[str] = mapped_column(String(255))
    exclusions: Mapped[str] = mapped_column(Text, default="")  # separadas por vírgula
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Sem grupos = vale para todos os grupos monitorados
    groups: Mapped[list[TgGroup]] = relationship(secondary=rule_groups, lazy="selectin")


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    source: Mapped[str] = mapped_column(String(16), default="telegram")
    group_title: Mapped[str] = mapped_column(String(255))
    keywords: Mapped[str] = mapped_column(String(500))
    snippet: Mapped[str] = mapped_column(Text)
    link: Mapped[str | None] = mapped_column(String(500))
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)


class SeenMessage(Base):
    __tablename__ = "seen_messages"
    __table_args__ = (UniqueConstraint("chat_id", "message_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    message_id: Mapped[int] = mapped_column(BigInteger)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
