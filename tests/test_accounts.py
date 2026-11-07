import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from price_watch import bot, fetcher, main, monitor, notifier
from price_watch.db import SessionLocal, init_db
from price_watch.models import Alert, Invite, Item, Product, Rule, Store, User
from price_watch.security import hash_password
from price_watch.telegram import GroupInfo, TelegramManager


def _user(name, admin=False, chat=None):
    with SessionLocal() as db:
        u = db.query(User).filter_by(username=name).first()
        if u is None:
            u = User(username=name, password_hash=hash_password("senha-" + name), is_admin=admin)
            db.add(u)
        u.telegram_chat_id = chat
        u.notify_channel = "telegram" if chat else "app"
        db.commit()
        return u.id


def _client(name):
    main._fails.clear()
    c = TestClient(main.app)
    r = c.post("/login", data={"username": name, "password": "senha-" + name})
    assert r.url.path in ("/products", "/ofertas")
    return c


@pytest.fixture(autouse=True)
def _db():
    init_db()


def test_convite_cria_conta_uma_vez_e_so_admin_gera():
    _user("chefe", admin=True)
    _user("comum")
    admin, comum = _client("chefe"), _client("comum")

    comum.post("/convites", data={"note": "x"})
    assert "Convites" not in comum.get("/telegram").text           # área de admin escondida
    assert comum.get("/groups", follow_redirects=False).headers["location"] == "/"
    assert comum.get("/stores", follow_redirects=False).status_code == 303

    admin.post("/convites", data={"note": "Maria"})
    with SessionLocal() as db:
        code = db.query(Invite).filter_by(note="Maria").order_by(Invite.id.desc()).first().code
    assert code in admin.get("/telegram").text

    novo = TestClient(main.app)
    r = novo.post(f"/convite/{code}", data={"username": "Maria", "password": "senha-maria1", "password2": "senha-maria1"})
    assert r.url.path == "/telegram" and "Bem-vindo" in r.text and "maria" in r.text
    outro = TestClient(main.app)
    r = outro.post(f"/convite/{code}", data={"username": "jose", "password": "senha-jose12", "password2": "senha-jose12"})
    assert "não vale mais" in r.text
    with SessionLocal() as db:
        assert db.query(User).filter_by(username="jose").count() == 0
        assert db.query(Invite).filter_by(code=code).one().used_by is not None


def test_cada_usuario_ve_so_o_que_e_seu_e_mesmo_link_pode_repetir(monkeypatch):
    async def no_check(pid):
        return None

    monkeypatch.setattr(monitor, "check_product", no_check)
    _user("ana")
    _user("beto")
    ana, beto = _client("ana"), _client("beto")
    link = "https://www.kabum.com.br/produto/777001/tv"
    ana.post("/products", data={"url": link, "target": "1000"})
    beto.post("/products", data={"url": link})
    with SessionLocal() as db:
        owners = [i.user_id for i in db.query(Item).join(Product).filter(Product.url == link)]
        ana_id = db.query(User).filter_by(username="ana").one().id
        ana_item = db.query(Item).filter_by(user_id=ana_id).join(Product).filter(Product.url == link).one().id
    assert len(owners) == 2 and len(set(owners)) == 2   # o mesmo link, um produto para cada um
    assert beto.get(f"/products/{ana_item}", follow_redirects=False).status_code == 303   # não vê o da Ana
    beto.post(f"/products/{ana_item}/delete")
    with SessionLocal() as db:
        assert db.get(Item, ana_item) is not None                                         # nem apaga
    assert "/products/%d" % ana_item in ana.get("/products").text


def test_bot_liga_e_desliga_a_conversa():
    uid = _user("carla")
    token = bot.link_token(uid)
    assert "abra o MeuPrecinho" in bot.handle_text("555", "/start codigo-errado")
    assert "Pronto, <b>carla</b>" in bot.handle_text("555", f"/start {token}")
    with SessionLocal() as db:
        u = db.get(User, uid)
        assert u.telegram_chat_id == "555" and u.tg_link_token is None
    assert "desligados" in bot.handle_text("555", "/parar")
    with SessionLocal() as db:
        assert db.get(User, uid).telegram_chat_id is None


def test_promocao_e_queda_de_preco_vao_para_a_conversa_de_cada_um(monkeypatch):
    sent = []

    async def fake_send(text, chat_id=None, **kw):
        sent.append((chat_id, text))
        return bool(chat_id)

    monkeypatch.setattr(notifier, "send", fake_send)
    a, b, c = _user("dani", chat="1001"), _user("edu", chat="1002"), _user("fabi")   # fabi sem Telegram
    with SessionLocal() as db:
        for uid, kw in ((a, "ps5 + slim"), (b, "ps5 + slim"), (c, "ps5 + slim")):
            db.add(Rule(user_id=uid, keyword=kw))
        db.commit()
    m = TelegramManager()
    m.reload()
    m.groups = {-100900: GroupInfo("Promos", "promos")}
    asyncio.run(m._on_message(SimpleNamespace(chat_id=-100900, id=5, raw_text="PS5 Slim por R$ 3.299")))
    assert sorted(str(chat) for chat, _ in sent) == ["1001", "1002"]  # ninguém recebe o de outro
    with SessionLocal() as db:
        assert db.query(Alert).filter_by(user_id=c, delivered=False).count() == 1  # fica no app da Fabi

    sent.clear()
    monitor.DOMAIN_GAP = 0

    async def fake_fetch(url, mode):
        return '<script type="application/ld+json">' + json.dumps(
            {"@type": "Product", "name": "PS5", "offers": {"price": 3000}}) + "</script>"

    monkeypatch.setattr(fetcher, "fetch", fake_fetch)
    with SessionLocal() as db:
        item = Item(user_id=b, name="PS5 Slim", target_cents=310000)
        offer = Product(item=item, store=Store(name="Loja Q", domain="loja-q-acc.com.br"), url="https://loja-q-acc.com.br/ps5")
        db.add_all([item, offer])
        db.commit()
        oid = offer.id
    asyncio.run(monitor.check_product(oid))
    assert [chat for chat, _ in sent] == ["1002"]
