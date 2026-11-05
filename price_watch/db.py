from collections.abc import Iterator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from .config import get_settings


class Base(DeclarativeBase):
    pass


engine = create_engine(
    get_settings().db_url,
    connect_args={"check_same_thread": False, "timeout": 15},
)


@event.listens_for(engine, "connect")
def _sqlite_pragmas(dbapi_conn, _):
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA journal_mode=WAL")
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


SessionLocal = sessionmaker(engine, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    with SessionLocal() as db:
        yield db


# Colunas acrescentadas depois da primeira versão (create_all não altera tabelas)
_NEW_COLUMNS = [
    ("stores", "price_selector", "VARCHAR(255)", None),
    # Ao surgir o coletor de casa, lojas conhecidas por bloquear servidores passam a usá-lo
    ("stores", "fetch_via", "VARCHAR(8) NOT NULL DEFAULT 'vm'",
     "UPDATE stores SET fetch_via = 'home' WHERE domain IN ({home})"),
    ("products", "source_url", "VARCHAR(2000)", None),
    ("products", "clicks", "INTEGER NOT NULL DEFAULT 0", None),
    ("products", "item_id", "INTEGER REFERENCES items(id) ON DELETE CASCADE", None),
    ("rules", "item_id", "INTEGER REFERENCES items(id) ON DELETE CASCADE", None),
    ("rules", "auto", "BOOLEAN NOT NULL DEFAULT 0", None),
    ("items", "watch_telegram", "BOOLEAN NOT NULL DEFAULT 1", None),
    ("items", "discovered_at", "DATETIME", None),
    ("items", "discovery_report", "TEXT", None),
    ("items", "zoom_url", "VARCHAR(500)", None),
    ("items", "pack_qty", "INTEGER", None),
    ("items", "zoom_checked_at", "DATETIME", None),
    ("products", "zoom_oid", "VARCHAR(30)", None),
    ("users", "is_admin", "BOOLEAN NOT NULL DEFAULT 0", None),
    ("users", "telegram_chat_id", "VARCHAR(32)", None),
    ("users", "tg_link_token", "VARCHAR(48)", None),
    ("users", "created_at", "DATETIME", None),
    ("items", "user_id", "INTEGER REFERENCES users(id) ON DELETE CASCADE", None),
    ("rules", "user_id", "INTEGER REFERENCES users(id) ON DELETE CASCADE", None),
    ("alerts", "user_id", "INTEGER REFERENCES users(id) ON DELETE CASCADE", None),
    ("rules", "max_price_cents", "INTEGER", None),
    ("alerts", "dedup_key", "VARCHAR(40)", None),
    ("alerts", "price_cents", "INTEGER", None),
    ("alerts", "also_in", "TEXT", None),
]


_DATA_MIGRATIONS = [
    ("mig_ml_api", "UPDATE stores SET fetch_via = 'api' WHERE domain = 'mercadolivre.com.br'"),
    # Cada produto antigo vira um item com uma oferta (mesmo id, para manter os links)
    ("mig_items",
     "INSERT INTO items (id, name, image_url, target_cents, best_cents, lowest_cents, "
     "last_notified_cents, active, created_at) "
     "SELECT id, name, image_url, target_cents, current_cents, lowest_cents, last_notified_cents, "
     "active, created_at FROM products WHERE item_id IS NULL"),
    ("mig_items_link", "UPDATE products SET item_id = id WHERE item_id IS NULL"),
    # Contas: quem já usava o app vira administrador e dono dos dados existentes
    ("mig_admin", "UPDATE users SET is_admin = 1 WHERE id = (SELECT MIN(id) FROM users)"),
    ("mig_owner_items", "UPDATE items SET user_id = (SELECT MIN(id) FROM users) WHERE user_id IS NULL"),
    ("mig_owner_rules", "UPDATE rules SET user_id = (SELECT MIN(id) FROM users) WHERE user_id IS NULL"),
    ("mig_owner_alerts", "UPDATE alerts SET user_id = (SELECT MIN(id) FROM users) WHERE user_id IS NULL"),
]


def _rebuild_products() -> None:
    """products.url era UNIQUE no app inteiro; agora é único por item. O SQLite não remove
    restrições, então a tabela é recriada seguindo o procedimento oficial do SQLite
    (chaves estrangeiras desligadas na MESMA conexão, para não reescrever as referências
    de price_points)."""
    from sqlalchemy.schema import CreateIndex, CreateTable

    from .models import Product

    raw = engine.raw_connection()
    try:
        con = raw.driver_connection
        sql = (con.execute("SELECT sql FROM sqlite_master WHERE name = 'products'").fetchone() or [""])[0] or ""
        flat = " ".join(sql.split())
        if "url VARCHAR(2000) NOT NULL UNIQUE" not in flat and "UNIQUE (url)" not in flat:
            return
        dialect = engine.dialect
        create_table = str(CreateTable(Product.__table__).compile(dialect=dialect))
        create_indexes = [str(CreateIndex(ix).compile(dialect=dialect)) for ix in Product.__table__.indexes]
        cols = [r[1] for r in con.execute("PRAGMA table_info(products)")]
        keep = ", ".join(c for c in cols if c in Product.__table__.c)

        old_isolation = con.isolation_level
        con.isolation_level = None  # controla a transação manualmente
        con.execute("PRAGMA foreign_keys = OFF")
        con.execute("BEGIN")
        try:
            con.execute(create_table.replace("CREATE TABLE products", "CREATE TABLE products_new", 1))
            con.execute(f"INSERT INTO products_new ({keep}) SELECT {keep} FROM products")
            con.execute("DROP TABLE products")
            con.execute("ALTER TABLE products_new RENAME TO products")
            for stmt in create_indexes:
                con.execute(stmt)
            if con.execute("PRAGMA foreign_key_check").fetchall():
                raise RuntimeError("chave estrangeira inválida após recriar products")
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
        finally:
            con.execute("PRAGMA foreign_keys = ON")
            con.isolation_level = old_isolation
    finally:
        raw.close()


def init_db() -> None:
    from . import models  # noqa: F401  (registra as tabelas)

    from .stores_catalog import HOME_DOMAINS

    Base.metadata.create_all(engine)
    _rebuild_products()
    home = ", ".join(f"'{d}'" for d in HOME_DOMAINS)
    with engine.begin() as conn:
        for table, column, ddl, after in _NEW_COLUMNS:
            cols = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if column not in cols:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                if after:
                    conn.exec_driver_sql(after.format(home=home))
        # Migrações de dados que rodam uma única vez
        done = {r[0] for r in conn.exec_driver_sql("SELECT key FROM settings WHERE key LIKE 'mig_%'")}
        for key, sql in _DATA_MIGRATIONS:
            if key not in done:
                conn.exec_driver_sql(sql)
                conn.exec_driver_sql("INSERT INTO settings (key, value) VALUES (?, '1')", (key,))
