from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    SECRET_KEY: str
    API_ID: int = 0
    API_HASH: str = ""
    BOT_TOKEN: str = ""
    CHAT_ID: str = ""

    DATA_DIR: Path = Path("data")
    COOKIE_SECURE: bool = False
    SESSION_DAYS: int = 30
    TIMEZONE: str = "America/Sao_Paulo"
    # App criado em developers.mercadolivre.com.br (API oficial de itens/preços)
    ML_CLIENT_ID: str = ""
    ML_CLIENT_SECRET: str = ""
    # Contato exigido pelo Web Push (notificações do app), ex.: mailto:voce@exemplo.com
    VAPID_SUBJECT: str = ""
    # Soft launch: convidados veem só Ofertas/Regras/Avisos; Produtos e Lojas ficam com o administrador
    PRODUCTS_FOR_USERS: bool = False
    # Códigos de afiliado do app (links de compra)
    AMAZON_TAG: str = ""      # ex.: meuprecinho-20
    ML_MATT_WORD: str = ""    # ex.: jldeiro
    ML_MATT_TOOL: str = ""    # ex.: 75607604

    @property
    def db_url(self) -> str:
        return f"sqlite:///{self.DATA_DIR / 'price_watch.db'}"

    @property
    def telethon_session(self) -> str:
        # Telethon acrescenta ".session" ao nome
        return str(self.DATA_DIR / "telegram")

    @property
    def telegram_configured(self) -> bool:
        return bool(self.API_ID and self.API_HASH)

    @property
    def bot_configured(self) -> bool:
        return bool(self.BOT_TOKEN and self.CHAT_ID)


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    if len(s.SECRET_KEY) < 16:
        raise RuntimeError("SECRET_KEY ausente ou curta demais (mínimo 16 caracteres)")
    s.DATA_DIR.mkdir(parents=True, exist_ok=True)
    return s
