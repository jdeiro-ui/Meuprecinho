import asyncio
import itertools
import random
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from price_watch import main, notifier, offers
from price_watch.db import SessionLocal, init_db
from price_watch.models import Alert, Offer, Rule, User
from price_watch.security import hash_password
from price_watch.telegram import GroupInfo, TelegramManager

MSG = """🔥 PROMOÇÃO IMPERDÍVEL 🔥
Kindle 16 GB (Geração 2024) Tela de 6" Preto
💰 R$ 399 à vista
🎟️ Cupom: KINDLE50
https://amzn.to/abc123
Entre no grupo: https://t.me/meucanal"""


def test_parse_extrai_nome_preco_cupom_e_loja():
    p = offers.parse(MSG)
    assert p.title.startswith("Kindle 16 GB")
    assert (p.price, p.coupon, p.store, p.url) == (39900, "KINDLE50", "Amazon", "https://amzn.to/abc123")


@pytest.mark.parametrize("text", [
    "Corre! https://t.me/canal R$ 10",                       # só link de canal
    "Galaxy A27 R$ 1.800 https://site-desconhecido.xyz/p",   # loja desconhecida
    "Air Fryer ótima https://amzn.to/x",                     # sem preço nem cupom
    "A https://amzn.to/1 B https://magalu.com/2 C https://kabum.com.br/3 R$ 9",  # lista de produtos
])
def test_parse_ignora_o_que_nao_e_oferta(text):
    assert offers.parse(text) is None


def test_cupom_nao_confunde_valor_em_reais():
    assert offers.parse("PS5 Slim R$ 3.299 com cupom de R$ 200 https://amzn.to/ps5").coupon is None


def test_scrub_remove_canal_e_mencao():
    assert offers.scrub("oferta @meucanal veja t.me/meucanal/123 agora") == "oferta veja agora"


@pytest.fixture
def clean():
    init_db()
    with SessionLocal() as db:
        db.query(Offer).delete()
        db.commit()


def test_mesma_oferta_em_dois_canais_vira_um_cartao_e_guarda_o_menor_preco(clean):
    offers.remember("Grupo A", MSG)
    offers.remember("Grupo B", MSG.replace("R$ 399", "R$ 379"))
    with SessionLocal() as db:
        (o,) = db.query(Offer).all()
        assert (o.price_cents, o.old_price_cents, o.seen_count, o.source_title) == (37900, 39900, 2, "Grupo A")


def test_aba_ofertas_so_admin_ve_o_canal_e_cliques_contam(clean):
    offers.remember("Canal Secreto", MSG)
    init_db()
    with SessionLocal() as db:
        for name, adm in (("adm_of", True), ("hosp_of", False)):
            if not db.query(User).filter_by(username=name).first():
                db.add(User(username=name, password_hash=hash_password("senha-" + name), is_admin=adm))
        db.commit()

    def login(name):
        main._fails.clear()
        c = TestClient(main.app)
        c.post("/login", data={"username": name, "password": "senha-" + name})
        return c

    hosp, adm = login("hosp_of"), login("adm_of")
    page = hosp.get("/ofertas").text
    assert "KINDLE50" in page and "R$ 399,00" in page and "comissão" in page and "Canal Secreto" not in page
    assert "Canal Secreto" in adm.get("/ofertas").text
    assert "Grupos" not in hosp.get("/ofertas").text.split('class="dock"')[1]      # convidado sem aba Grupos
    assert "Produtos" in adm.get("/ofertas").text.split('class="dock"')[1]

    with SessionLocal() as db:
        oid = db.query(Offer).one().id
    r = hosp.get(f"/o/{oid}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"] == "https://amzn.to/abc123"
    assert hosp.get("/ofertas?q=kindle").text.count("offer-title") == 1
    assert hosp.get("/ofertas?q=geladeira").text.count("offer-title") == 0

    hosp.post(f"/ofertas/{oid}/seguir")
    with SessionLocal() as db:
        r = db.query(Rule).join(User).filter(User.username == "hosp_of").one()
        assert "kindle" in r.keyword and r.max_price_cents == 39900


def test_produtos_fechado_para_convidado(monkeypatch):
    from price_watch.config import get_settings
    monkeypatch.setattr(get_settings(), "PRODUCTS_FOR_USERS", False)
    init_db()
    with SessionLocal() as db:
        if not db.query(User).filter_by(username="hosp2").first():
            db.add(User(username="hosp2", password_hash=hash_password("senha-hosp2")))
            db.commit()
    main._fails.clear()
    c = TestClient(main.app)
    c.post("/login", data={"username": "hosp2", "password": "senha-hosp2"})
    r = c.get("/products", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/ofertas"
    assert c.get("/", follow_redirects=False).headers["location"] == "/ofertas"


def test_aviso_do_convidado_nao_revela_o_canal(monkeypatch, clean):
    sent = []

    async def fake_send(text, chat_id=None, **kw):
        sent.append(text)
        return True

    monkeypatch.setattr(notifier, "send", fake_send)
    init_db()
    with SessionLocal() as db:
        db.query(Alert).delete()
        db.query(Rule).delete()
        u = db.query(User).filter_by(username="hosp3").first() or \
            User(username="hosp3", password_hash=hash_password("senha-hosp3"), telegram_chat_id="88",
                 notify_channel="telegram")
        db.add(u)
        db.flush()
        db.add(Rule(user_id=u.id, keyword="kindle"))
        db.commit()
        uid = u.id
    m = TelegramManager()
    m.reload()
    m.groups = {-5001: GroupInfo("Canal Secreto", "canalsecreto")}
    asyncio.run(m._on_message(SimpleNamespace(chat_id=-5001, id=random.randint(10**6, 10**9), raw_text=MSG)))
    assert sent and all("Canal Secreto" not in t and "meucanal" not in t for t in sent)
    assert "Ver oferta" in sent[0] and "https://amzn.to/abc123" in sent[0]
    with SessionLocal() as db:
        a = db.query(Alert).filter_by(user_id=uid).one()
        assert a.group_title == offers.GENERIC_SOURCE and "t.me" not in (a.link or "") and "Canal" not in a.snippet
        assert db.query(Offer).count() == 1                      # a mesma mensagem também alimentou o feed
