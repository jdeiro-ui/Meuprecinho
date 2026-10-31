"""Envio de avisos pelo Bot API do Telegram (httpx)."""

import asyncio
import html
import logging

import httpx

from .config import get_settings

log = logging.getLogger(__name__)

_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None or _client.is_closed:
        _client = httpx.AsyncClient(timeout=20)
    return _client


async def close() -> None:
    if _client is not None:
        await _client.aclose()


def format_alert(group_title: str, keywords: list[str], text: str, link: str | None) -> str:
    e = html.escape
    parts = [
        f"🔔 <b>{e(group_title)}</b>",
        f"Palavra: <i>{e(', '.join(keywords))}</i>",
        "",
        e(text),
    ]
    if link:
        parts += ["", f'<a href="{e(link, quote=True)}">Abrir mensagem</a>']
    return "\n".join(parts)


async def send(text_html: str, retries: int = 3) -> bool:
    s = get_settings()
    if not s.bot_configured:
        log.warning("Bot não configurado (BOT_TOKEN/CHAT_ID); aviso não enviado")
        return False
    url = f"https://api.telegram.org/bot{s.BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": s.CHAT_ID,
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
                return True
            # Só o status: a resposta pode ecoar o conteúdo da mensagem
            log.warning("Bot API respondeu %s", r.status_code)
            if r.status_code < 500:
                return False
        except httpx.HTTPError as exc:
            log.warning("Falha ao chamar o Bot API (%s), tentativa %d", type(exc).__name__, attempt)
        await asyncio.sleep(2**attempt)
    return False
