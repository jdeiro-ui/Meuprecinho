"""Envio de avisos pelo Bot API do Telegram (httpx)."""

import asyncio
import html
import logging

import httpx

from .config import get_settings

log = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None
# Motivo da última falha (ex.: "Unauthorized"), mostrado no teste do painel
last_error: str | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=20)
    return _client


async def close() -> None:
    if _client is not None:
        await _client.aclose()


def format_alert(group_title: str, keywords: list[str], text: str, link: str | None,
                 extra: list[str] | None = None) -> str:
    """extra: linhas já em HTML (ex.: comparação com o produto acompanhado)."""
    e = html.escape
    parts = [
        f"🔔 <b>{e(group_title)}</b>",
        f"Palavra: <i>{e(', '.join(keywords))}</i>",
        *(["", *extra] if extra else []),
        "",
        e(text),
    ]
    if link:
        parts += ["", f'<a href="{e(link, quote=True)}">Abrir mensagem</a>']
    return "\n".join(parts)


def format_price_alert(store: str, name: str, old: int | None, new: int, target: int | None,
                       reason: str, url: str) -> str:
    from .pricing import format_brl

    e = html.escape
    price = f"<b>{format_brl(new)}</b>"
    if old and old != new:
        price = f"<s>{format_brl(old)}</s> → {price}"
    parts = [f"💰 <b>{e(name)}</b>", f"{e(store)} · {e(reason)}", "", price]
    if target:
        parts.append(f"Seu alvo: {format_brl(target)}")
    parts += ["", f'<a href="{e(url, quote=True)}">Comprar na loja</a>', "<i>link de afiliado do MeuPrecinho</i>"]
    return "\n".join(parts)


async def deliver(user_id: int | None, telegram_html: str, title: str, body: str,
                  url: str = "/alerts", tag: str | None = None) -> bool:
    """Envia o aviso pelo canal que o usuário escolheu (app, Telegram ou ambos).
    True se chegou por pelo menos um canal."""
    from .db import SessionLocal
    from .models import User
    from . import push

    if user_id is None:
        return await send(telegram_html)
    with SessionLocal() as db:
        user = db.get(User, user_id)
        channel = (user.notify_channel if user else None) or "app"
        chat = user.telegram_chat_id if user else None
    ok = False
    if channel in ("telegram", "both") and chat:
        ok = await send(telegram_html, chat_id=chat) or ok
    if channel in ("app", "both"):
        ok = await push.send_to_user(user_id, title, body, url, tag) or ok
    return ok


def admin_user_id() -> int | None:
    from sqlalchemy import select

    from .db import SessionLocal
    from .models import User

    with SessionLocal() as db:
        return db.scalar(select(User.id).where(User.is_admin).order_by(User.id))


def admin_chat() -> str | None:
    """Conversa do administrador (avisos técnicos, ex.: coletor parado)."""
    from sqlalchemy import select

    from .db import SessionLocal
    from .models import User

    with SessionLocal() as db:
        chat = db.scalar(select(User.telegram_chat_id).where(User.is_admin, User.telegram_chat_id.is_not(None)))
    return chat or get_settings().CHAT_ID or None


async def send(text_html: str, chat_id: str | None = None, retries: int = 3) -> bool:
    """Envia para a conversa do usuário. Sem conversa conectada, não envia (nunca cai na
    conversa de outra pessoa)."""
    global last_error
    s = get_settings()
    if not s.BOT_TOKEN:
        log.warning("Bot não configurado (BOT_TOKEN); aviso não enviado")
        return False
    if not chat_id:
        last_error = "usuário sem Telegram conectado"
        return False
    url = f"https://api.telegram.org/bot{s.BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text_html,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    for attempt in range(1, retries + 1):
        try:
            r = await _http().post(url, json=payload)
            if r.status_code == 429:
                wait = r.json().get("parameters", {}).get("retry_after", 5)
                await asyncio.sleep(min(int(wait), 60))
                continue
            if r.is_success:
                last_error = None
                return True
            # Só o status no log: a resposta pode ecoar o conteúdo da mensagem
            log.warning("Bot API respondeu %s", r.status_code)
            try:
                last_error = r.json().get("description")
            except ValueError:
                last_error = f"HTTP {r.status_code}"
            if r.status_code < 500:
                return False
        except httpx.HTTPError as exc:
            log.warning("Falha ao chamar o Bot API (%s), tentativa %d", type(exc).__name__, attempt)
            last_error = f"sem conexão ({type(exc).__name__})"
        await asyncio.sleep(2**attempt)
    return False
