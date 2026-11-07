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
    # Administrador: escolhe grupos e lojas, conecta Telegram/Mercado Livre, gera convites
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    # Conversa do usuário com o bot (avisos); preenchida ao tocar no link t.me/<bot>?start=<token>
    telegram_chat_id: Mapped[str | None] = mapped_column(String(32))
    tg_link_token: Mapped[str | None] = mapped_column(String(48), index=True)
    # Onde chegam os avisos: "app" (notificação do celular), "telegram" ou "both"
    notify_channel: Mapped[str] = mapped_column(String(10), default="app", server_default="app")
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), default=utcnow)


class PushSubscription(Base):
    """Aparelho em que o usuário ativou as notificações do app (Web Push)."""

    __tablename__ = "push_subscriptions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    endpoint: Mapped[str] = mapped_column(String(1000), unique=True)
    p256dh: Mapped[str] = mapped_column(String(200))
    auth: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_ok_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Invite(Base):
    """Convite para criar conta (lançamento fechado)."""

    __tablename__ = "invites"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    note: Mapped[str] = mapped_column(String(120), default="")  # para quem é
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    used_by: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    name: Mapped[str | None] = mapped_column(String(300))
    image_url: Mapped[str | None] = mapped_column(String(2000))
    # Identificadores para achar o mesmo produto em outras lojas
    gtin: Mapped[str | None] = mapped_column(String(20), index=True)  # EAN/código de barras
    brand: Mapped[str | None] = mapped_column(String(120))
    model: Mapped[str | None] = mapped_column(String(120))
    # Unidades na embalagem (caixa com 12 = 12). Vazio = lido do nome do produto
    pack_qty: Mapped[int | None] = mapped_column(Integer)
    # Valores em centavos, considerando a melhor oferta entre as lojas
    target_cents: Mapped[int | None] = mapped_column(Integer)
    best_cents: Mapped[int | None] = mapped_column(Integer)
    lowest_cents: Mapped[int | None] = mapped_column(Integer)
    last_notified_cents: Mapped[int | None] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    watch_telegram: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")  # procurar nos grupos
    discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # busca em lojas
    discovery_report: Mapped[str | None] = mapped_column(Text)  # resultado da última busca, por loja
    zoom_url: Mapped[str | None] = mapped_column(String(500))   # página do produto (ou busca) no Zoom
    zoom_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    offers: Mapped[list["Product"]] = relationship(
        back_populates="item", cascade="all, delete-orphan", passive_deletes=True, lazy="selectin"
    )

    @property
    def units(self) -> int:
        """Unidades por embalagem (informada ou lida do nome; 1 se não houver)."""
        from .pricing import pack_qty

        return self.pack_qty or pack_qty(self.name)

    def best_offer(self) -> "Product | None":
        live = [o for o in self.offers
                if o.current_cents and o.available is not False and not o.last_error]
        return min(live, key=lambda o: o.current_cents) if live else None


class Product(Base):
    """Oferta: o produto numa loja específica (link, preço e leitura próprios)."""

    __tablename__ = "products"
    __table_args__ = (UniqueConstraint("item_id", "url"),)  # o mesmo link pode estar em itens de usuários diferentes

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(String(2000), index=True)
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
    # Oferta lida pelo comparador Zoom (id da oferta lá); o preço vem da página do Zoom
    zoom_oid: Mapped[str | None] = mapped_column(String(30))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    last_error: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    store: Mapped[Store] = relationship(back_populates="products", lazy="joined")
    item: Mapped[Item | None] = relationship(back_populates="offers")


class Suggestion(Base):
    """Possível oferta do mesmo produto em outra loja, aguardando confirmação."""

    __tablename__ = "suggestions"
    __table_args__ = (UniqueConstraint("item_id", "url"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    url: Mapped[str] = mapped_column(String(2000))
    store_name: Mapped[str] = mapped_column(String(120))
    title: Mapped[str] = mapped_column(String(300))
    price_cents: Mapped[int | None] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(12), default="pending")  # pending | rejected
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


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
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    # Regra criada a partir de um produto (busca do produto nos grupos do Telegram)
    item_id: Mapped[int | None] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    auto: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")  # palavra-chave gerada pelo app
    # Teto: só avisa promoção até este preço (regra de produto usa o preço-alvo do produto)
    max_price_cents: Mapped[int | None] = mapped_column(Integer)
    item: Mapped[Item | None] = relationship(lazy="joined")


class Alert(Base):
    __tablename__ = "alerts"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    source: Mapped[str] = mapped_column(String(16), default="telegram")
    group_title: Mapped[str] = mapped_column(String(255))
    keywords: Mapped[str] = mapped_column(String(500))
    snippet: Mapped[str] = mapped_column(Text)
    link: Mapped[str | None] = mapped_column(String(500))
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)
    # Promoção repetida em outros grupos: chave (item:<id> / rule:<id>), preço e onde mais apareceu
    dedup_key: Mapped[str | None] = mapped_column(String(40), index=True)
    price_cents: Mapped[int | None] = mapped_column(Integer)
    also_in: Mapped[str | None] = mapped_column(Text)


class Offer(Base):
    """Promoção encontrada nos canais monitorados (aba Ofertas, para todos os usuários)."""
    __tablename__ = "offers"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    key: Mapped[str] = mapped_column(String(160), index=True)  # produto/link normalizado
    title_key: Mapped[str] = mapped_column(String(100), index=True, default="")
    title: Mapped[str] = mapped_column(String(120))
    search: Mapped[str] = mapped_column(String(200), default="")  # título sem acento, para a busca
    price_cents: Mapped[int | None] = mapped_column(Integer)
    old_price_cents: Mapped[int | None] = mapped_column(Integer)
    coupon: Mapped[str | None] = mapped_column(String(40))
    store: Mapped[str] = mapped_column(String(40))
    url: Mapped[str] = mapped_column(String(700))
    image: Mapped[bool] = mapped_column(Boolean, default=False)
    source_title: Mapped[str] = mapped_column(String(255), default="")  # só o administrador vê
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    seen_count: Mapped[int] = mapped_column(Integer, default=1)


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
