"""Notificações do app (Web Push): chaves VAPID e envio para os aparelhos do usuário."""

import asyncio
import base64
import json
import logging

from sqlalchemy import select

from .config import get_settings
from .db import SessionLocal
from .models import PushSubscription, Setting, utcnow

log = logging.getLogger(__name__)
_vapid = None


def _webpush():
    """pywebpush.webpush (trocável nos testes)."""
    from pywebpush import webpush

    return webpush


def available() -> bool:
    try:
        import pywebpush  # noqa: F401
    except ImportError:
        return False
    return True


def _keys():
    """Chave VAPID do servidor: gerada uma vez e guardada no banco."""
    global _vapid
    if _vapid is None:
        from py_vapid import Vapid01

        with SessionLocal() as db:
            row = db.get(Setting, "vapid_private_pem")
            if row is None:
                v = Vapid01()
                v.generate_keys()
                row = Setting(key="vapid_private_pem", value=v.private_pem().decode())
                db.add(row)
                db.commit()
            _vapid = Vapid01.from_pem(row.value.encode())
    return _vapid


def public_key() -> str:
    """Chave pública no formato que o navegador pede (applicationServerKey)."""
    from cryptography.hazmat.primitives import serialization

    raw = _keys().public_key.public_bytes(serialization.Encoding.X962,
                                          serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def remember_app_url(url: str) -> None:
    with SessionLocal() as db:
        row = db.get(Setting, "app_url")
        if row is None:
            db.add(Setting(key="app_url", value=url))
            db.commit()
        elif row.value != url:  # o endereço do app mudou (ex.: saída do Tailscale para o domínio público)
            row.value = url
            db.commit()


def _subject() -> str:
    s = get_settings().VAPID_SUBJECT
    if s:
        return s
    with SessionLocal() as db:
        row = db.get(Setting, "app_url")
    return row.value if row else "https://localhost"


def subscriptions(user_id: int) -> list[PushSubscription]:
    with SessionLocal() as db:
        return db.scalars(select(PushSubscription).where(PushSubscription.user_id == user_id)).all()


def _send_one(sub: PushSubscription, data: str) -> bool | None:
    """True = entregue; False = falhou; None = aparelho saiu (assinatura apagada)."""
    from pywebpush import WebPushException

    try:
        _webpush()(subscription_info={"endpoint": sub.endpoint, "keys": {"p256dh": sub.p256dh, "auth": sub.auth}},
                   data=data, vapid_private_key=_keys(), vapid_claims={"sub": _subject()}, ttl=86400)
        return True
    except WebPushException as e:
        status = getattr(e.response, "status_code", None)
        if status in (404, 410):  # o usuário desativou ou desinstalou: some da lista
            return None
        log.info("Push falhou (HTTP %s)", status)
        return False
    except Exception as e:  # noqa: BLE001 — rede etc.
        log.info("Push falhou (%s)", type(e).__name__)
        return False


async def send_to_user(user_id: int, title: str, body: str, url: str = "/alerts", tag: str | None = None) -> bool:
    """Notifica todos os aparelhos do usuário. True se ao menos um recebeu."""
    if not available():
        return False
    subs = await asyncio.to_thread(subscriptions, user_id)
    if not subs:
        return False
    data = json.dumps({"title": title[:120], "body": body[:300], "url": url, "tag": tag})
    ok = False
    for sub in subs:
        result = await asyncio.to_thread(_send_one, sub, data)
        await asyncio.to_thread(_record, sub.id, result)
        ok = ok or bool(result)
    return ok


def _record(sub_id: int, result: bool | None) -> None:
    with SessionLocal() as db:
        sub = db.get(PushSubscription, sub_id)
        if sub is None:
            return
        if result is None:
            db.delete(sub)
        elif result:
            sub.last_ok_at = utcnow()
        db.commit()
