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
]


def init_db() -> None:
    from . import models  # noqa: F401  (registra as tabelas)

    from .stores_catalog import HOME_DOMAINS

    Base.metadata.create_all(engine)
    home = ", ".join(f"'{d}'" for d in HOME_DOMAINS)
    with engine.begin() as conn:
        for table, column, ddl, after in _NEW_COLUMNS:
            cols = {row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})")}
            if column not in cols:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                if after:
                    conn.exec_driver_sql(after.format(home=home))
