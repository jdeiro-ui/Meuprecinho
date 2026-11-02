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
    # Seletor CSS opcional para lojas em que a leitura automática falha
    price_selector: Mapped[str | None] = mapped_column(String(255))
    # Quem baixa a página: "vm" (o próprio servidor) ou "home" (coletor no celular de casa,
    # com IP residencial, para lojas que bloqueiam servidores na nuvem)
    fetch_via: Mapped[str] = mapped_column(String(8), default="vm", server_default="vm")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    products: Mapped[list["Product"]] = relationship(
        back_populates="store", cascade="all, delete-orphan", passive_deletes=True
    )


class Item(Base):
    """O que o usuário quer comprar (ex.: uma TV). Tem uma oferta por loja (Product)."""

    __tablename__ = "items"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str | None] = mapped_column(String(300))
    image_url: Mapped[str | None] = mapped_column(String(2000))
    # Identificadores para achar o mesmo produto em outras lojas
    gtin: Mapped[str | None] = mapped_column(String(20), index=True)  # EAN/código de barras
    brand: Mapped[str | None] = mapped_column(String(120))
    model: Mapped[str | None] = mapped_column(String(120))
    # Valores em centavos, considerando a melhor oferta entre as lojas
    target_cents: Mapped[int | None] = mapped_column(Integer)
    best_cents: Mapped[int | None] = mapped_column(Integer)
    lowest_cents: Mapped[int | None] = mapped_column(Integer)
    last_notified_cents: Mapped[int | None] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    watch_telegram: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")  # procurar nos grupos
    discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # busca em lojas
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    offers: Mapped[list["Product"]] = relationship(
        back_populates="item", cascade="all, delete-orphan", passive_deletes=True, lazy="selectin"
    )

    def best_offer(self) -> "Product | None":
        live = [o for o in self.offers
                if o.current_cents and o.available is not False and not o.last_error]
        return min(live, key=lambda o: o.current_cents) if live else None


class Product(Base):
    """Oferta: o produto numa loja específica (link, preço e leitura próprios)."""

    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(String(2000), unique=True)
    # Link original colado pelo usuário quando era curto/de afiliado (ex.: meli.la).
    # É usado no "Ver na loja" e, no Mercado Livre, para ler o preço.
    source_url: Mapped[str | None] = mapped_column(String(2000))
    name: Mapped[str | None] = mapped_column(String(300))
    image_url: Mapped[str | None] = mapped_column(String(2000))
    # Valores em centavos
    target_cents: Mapped[int | None] = mapped_column(Integer)
    current_cents: Mapped[int | None] = mapped_column(Integer)
    lowest_cents: Mapped[int | None] = mapped_column(Integer)
    last_notified_cents: Mapped[int | None] = mapped_column(Integer)
    available: Mapped[bool | None] = mapped_column(Boolean)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    clicks: Mapped[int] = mapped_column(Integer, default=0, server_default="0")  # cliques em "Comprar"
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_error: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    store: Mapped[Store] = relationship(back_populates="products", lazy="joined")
    item: Mapped[Item | None] = relationship(back_populates="offers")


class PricePoint(Base):
    __tablename__ = "price_points"

    id: Mapped[int] = mapped_column(primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id", ondelete="CASCADE"), index=True)
    cents: Mapped[int] = mapped_column(Integer)
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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
    # Regra criada a partir de um produto (busca do produto nos grupos do Telegram)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    auto: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")  # palavra-chave gerada pelo app
    item: Mapped[Item | None] = relationship(lazy="joined")


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


class Setting(Base):
    """Configurações guardadas no banco (ex.: token do coletor de casa)."""

    __tablename__ = "settings"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[str] = mapped_column(Text)
