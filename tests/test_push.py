import asyncio
import json

import pytest
from fastapi.testclient import TestClient

from price_watch import main, notifier, push
from price_watch.db import SessionLocal, init_db
from price_watch.models import PushSubscription, User
from price_watch.security import hash_password


class FakeResp:
    def __init__(self, status):
        self.status_code = status


@pytest.fixture
def user():
    init_db()
    with SessionLocal() as db:
        u = db.query(User).filter_by(username="pushy").first() or \
            User(username="pushy", password_hash=hash_password("senha-pushy1"))
        db.add(u)
        db.flush()
        db.query(PushSubscription).filter_by(user_id=u.id).delete()
        u.notify_channel, u.telegram_chat_id = "app", "321"
        db.commit()
        return u.id


def _login():
    main._fails.clear()
    c = TestClient(main.app)
    c.post("/login", data={"username": "pushy", "password": "senha-pushy1"})
    return c


def test_chave_publica_e_cadastro_do_aparelho(user):
    c = _login()
    key = c.get("/push/key").json()["key"]
    assert len(key) > 80 and key == push.public_key()            # mesma chave sempre (guardada no banco)
    sub = {"endpoint": "https://fcm.googleapis.com/fcm/send/abc", "keys": {"p256dh": "P" * 87, "auth": "A" * 22}}
    assert c.post("/push/subscribe", json=sub).json() == {"ok": True}
    assert c.post("/push/subscribe", json=sub).json() == {"ok": True}   # de novo não duplica
    assert len(push.subscriptions(user)) == 1
    assert "1 aparelho" in c.get("/telegram").text
    assert c.post("/push/subscribe", json={"endpoint": "http://x"}).status_code == 400
    c.post("/push/unsubscribe", json={"endpoint": sub["endpoint"]})
    assert push.subscriptions(user) == []


def test_canal_escolhido_decide_por_onde_sai(user, monkeypatch):
    with SessionLocal() as db:
        db.add(PushSubscription(user_id=user, endpoint="https://push.example/1", p256dh="p", auth="a"))
        db.add(PushSubscription(user_id=user, endpoint="https://push.example/gone", p256dh="p", auth="a"))
        db.commit()
    pushed, telegram = [], []

    def fake_webpush(subscription_info, data, **kw):
        from pywebpush import WebPushException
        if subscription_info["endpoint"].endswith("gone"):
            raise WebPushException("sumiu", response=FakeResp(410))
        pushed.append(json.loads(data))

    async def fake_send(text, chat_id=None, **kw):
        telegram.append(chat_id)
        return True

    monkeypatch.setattr(push, "_webpush", lambda: fake_webpush)
    monkeypatch.setattr(notifier, "send", fake_send)

    def go():
        return asyncio.run(notifier.deliver(user, "<b>oi</b>", title="T", body="B", url="/products/1", tag="k"))

    assert go() and telegram == [] and pushed[0] == {"title": "T", "body": "B", "url": "/products/1", "tag": "k"}
    assert [s.endpoint for s in push.subscriptions(user)] == ["https://push.example/1"]   # 410: removida

    with SessionLocal() as db:
        db.get(User, user).notify_channel = "telegram"
        db.commit()
    pushed.clear()
    assert go() and telegram == ["321"] and pushed == []

    with SessionLocal() as db:
        db.get(User, user).notify_channel = "both"
        db.commit()
    telegram.clear()
    assert go() and telegram == ["321"] and len(pushed) == 1


def test_troca_de_canal_pela_conta(user):
    c = _login()
    c.post("/conta/notificacoes", data={"channel": "both"})
    with SessionLocal() as db:
        assert db.get(User, user).notify_channel == "both"
    page = c.get("/telegram").text
    assert "Ativar neste celular" in page and "Telegram" in page


def test_conectar_bot_com_canal_app_passa_a_ambos(user):
    from price_watch import bot

    with SessionLocal() as db:
        u = db.get(User, user)
        u.telegram_chat_id, u.notify_channel = None, "app"
        db.commit()
    token = bot.link_token(user)
    bot.handle_text("999", f"/start {token}")
    with SessionLocal() as db:
        assert db.get(User, user).notify_channel == "both"


def test_endereco_do_app_e_atualizado_quando_muda():
    init_db()
    push.remember_app_url("https://antigo.ts.net")
    push.remember_app_url("https://meuprecinho.com.br")
    with SessionLocal() as db:
        from price_watch.models import Setting
        assert db.get(Setting, "app_url").value == "https://meuprecinho.com.br"
