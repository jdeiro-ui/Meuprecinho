"""Bot do Telegram: liga cada usuário à sua conversa (t.me/<bot>?start=<código>).

Um laço de "long polling" (getUpdates) recebe o /start com o código gerado na tela
Conta e grava a conversa do usuário. /parar desliga os avisos."""

import asyncio
import logging
import secrets

import httpx
from sqlalchemy import select

from .config import get_settings
from .db import SessionLocal
from .models import User

log = logging.getLogger(__name__)
_username: str | None = None
transport: httpx.AsyncBaseTransport | None = None  # para testes


def _api(method: str) -> str:
    return f"https://api.telegram.org/bot{get_settings().BOT_TOKEN}/{method}"


async def username() -> str | None:
    """@ do bot (para montar o link de conexão)."""
    global _username
    if _username is None and get_settings().BOT_TOKEN:
        try:
            async with httpx.AsyncClient(timeout=15, transport=transport) as c:
                r = await c.get(_api("getMe"))
            if r.status_code == 200:
                _username = r.json()["result"]["username"]
        except (httpx.HTTPError, KeyError, ValueError):
            pass
    return _username


def link_token(user_id: int) -> str:
    with SessionLocal() as db:
        user = db.get(User, user_id)
        if not user.tg_link_token:
            user.tg_link_token = secrets.token_urlsafe(16)
            db.commit()
        return user.tg_link_token


async def connect_link(user_id: int) -> str | None:
    name = await username()
    return f"https://t.me/{name}?start={link_token(user_id)}" if name else None


def handle_text(chat_id: str, text: str) -> str | None:
    """Processa uma mensagem recebida pelo bot e devolve a resposta (ou None)."""
    text = (text or "").strip()
    with SessionLocal() as db:
        if text.startswith("/start"):
            token = text.partition(" ")[2].strip()
            user = db.scalar(select(User).where(User.tg_link_token == token)) if token else None
            if user is None:
                return ("Olá! Para receber os avisos, abra o MeuPrecinho, vá em <b>Conta</b> e toque em "
                        "<b>Conectar Telegram</b>.")
            for other in db.scalars(select(User).where(User.telegram_chat_id == chat_id, User.id != user.id)):
                other.telegram_chat_id = None  # uma conversa pertence a uma conta só
            user.telegram_chat_id, user.tg_link_token = chat_id, None
            db.commit()
            return (f"✅ Pronto, <b>{user.username}</b>! Seus avisos do MeuPrecinho vão chegar aqui.\n"
                    "Para parar, envie /parar.")
        if text.startswith("/parar") or text.startswith("/stop"):
            for user in db.scalars(select(User).where(User.telegram_chat_id == chat_id)):
                user.telegram_chat_id = None
            db.commit()
            return "Avisos desligados. Para voltar, conecte de novo pela tela Conta do app."
    return None


async def run() -> None:
    """Laço de recebimento (roda enquanto o app estiver de pé)."""
    from . import notifier
    from .telegram import manager

    offset, backoff = 0, 5
    while True:
        try:
            async with httpx.AsyncClient(timeout=40, transport=transport) as c:
                r = await c.get(_api("getUpdates"), params={"timeout": 30, "offset": offset,
                                                             "allowed_updates": '["message"]'})
            if r.status_code != 200:
                raise httpx.HTTPError(f"HTTP {r.status_code}")
            backoff = 5
            for upd in r.json().get("result", []):
                offset = upd["update_id"] + 1
                msg = upd.get("message") or {}
                chat_id = str((msg.get("chat") or {}).get("id") or "")
                if not chat_id or (msg.get("chat") or {}).get("type") != "private":
                    continue
                reply = await asyncio.to_thread(handle_text, chat_id, msg.get("text") or "")
                if reply:
                    await notifier.send(reply, chat_id=chat_id)
                    await asyncio.to_thread(manager.reload)  # conversas dos usuários mudaram
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 — sem rede etc.: tenta de novo
            log.info("Bot: recebimento falhou (%s); nova tentativa em %ss", type(e).__name__, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 300)
