import asyncio
import itertools
import random
from datetime import timedelta
from types import SimpleNamespace

import pytest

from price_watch import notifier
from price_watch.db import SessionLocal, init_db
from price_watch.item_rules import sync
from price_watch.models import Alert, Item, Product, Rule, Store, User
from price_watch.pricing import price_in_text
from price_watch.security import hash_password
from price_watch.telegram import GroupInfo, TelegramManager


@pytest.mark.parametrize("text, cents", [
    ("De R$ 2.299 por R$ 1.799,90 no Pix", 179990),
    ("Galaxy A27 R$1.849 ou 10x de R$ 199,90", 184900),
    ("PS5 R$ 3.299 com cupom de R$ 200", 329900),
    ("Use R$ 50 OFF: Kindle R$ 549", 54900),
    ("Air Fryer R$ 299 + R$ 30 de cashback", 29900),
    ("Economize R$ 400! Echo Dot R$ 249", 24900),
    ("Frete de R$ 19,90; TV R$ 2.599", 259900),
    ("em 12x R$ 166,58 sem juros — Galaxy R$ 1.999", 199900),
    ("corre que tá barato", None),
])
def test_price_in_text_ignora_parcela_cupom_cashback_frete(text, cents):
    assert price_in_text(text) == cents


@pytest.fixture
def env(monkeypatch):
    init_db()
    sent = []

    async def fake_send(text, chat_id=None, **kw):
        sent.append(text)
        return True

    monkeypatch.setattr(notifier, "send", fake_send)
    with SessionLocal() as db:
        db.query(Alert).delete()
        db.query(Rule).delete()
        u = db.query(User).filter_by(username="teto").first() or \
            User(username="teto", password_hash=hash_password("senha-teto-1"), telegram_chat_id="77",
                 notify_channel="telegram", is_admin=True)  # admin vê o nome do grupo
        u.is_admin = True
        db.add(u)
        db.flush()
        db.query(Item).filter_by(user_id=u.id).delete()
        store = db.query(Store).filter_by(domain="loja-teto.com.br").first() or \
            Store(name="Loja T", domain="loja-teto.com.br")
        item = Item(user_id=u.id, name="Console Nintendo Switch 2", brand="Nintendo", target_cents=380000)
        db.add_all([item, Product(item=item, store=store, url="https://loja-teto.com.br/sw2")])
        db.flush()
        sync(db, item)
        db.add(Rule(user_id=u.id, keyword="air + fryer", max_price_cents=30000))
        db.commit()
        uid = u.id
    m = TelegramManager()
    m.reload()
    m.groups = {-1001: GroupInfo("Grupo A", "grupoa"), -1002: GroupInfo("Grupo B", None),
                -1003: GroupInfo("Grupo C", None)}
    counter = itertools.count(random.randint(10**6, 10**9))  # ids únicos: seen_messages é compartilhado

    def msg(group, text):
        asyncio.run(m._on_message(SimpleNamespace(chat_id=group, id=next(counter), raw_text=text)))

    return SimpleNamespace(sent=sent, msg=msg, uid=uid)


def test_teto_do_produto_e_da_regra_manual(env):
    env.msg(-1001, "Nintendo Switch 2 por R$ 4.199")          # acima do alvo (R$ 3.800): não avisa
    assert env.sent == []
    env.msg(-1001, "Nintendo Switch 2 por R$ 3.699 no Pix")   # abaixo: avisa
    assert len(env.sent) == 1 and "Seu produto" in env.sent[0]
    env.msg(-1001, "Air Fryer Mondial R$ 349")                # regra manual até R$ 300: não avisa
    env.msg(-1002, "Air Fryer Mondial R$ 279")
    assert len(env.sent) == 2


def test_sem_preco_avisa_marcando(env):
    env.msg(-1001, "Corre! Nintendo Switch 2 muito barato no link")
    assert len(env.sent) == 1 and "Preço não informado" in env.sent[0]


def test_repetida_em_outros_grupos_avisa_uma_vez_e_de_novo_se_baixar(env):
    env.msg(-1001, "Nintendo Switch 2 por R$ 3.699")
    env.msg(-1002, "🔥 Nintendo Switch 2 R$ 3.699 no Pix")    # mesma promoção, outro grupo
    env.msg(-1003, "Nintendo Switch 2 por R$ 3.690")          # menos de 1% abaixo: ainda repetida
    assert len(env.sent) == 1
    with SessionLocal() as db:
        first = db.query(Alert).filter_by(user_id=env.uid).one()
        assert first.also_in == "Grupo B, Grupo C"
    env.msg(-1002, "Baixou! Nintendo Switch 2 R$ 3.499")      # caiu ≥ 1%: avisa de novo
    assert len(env.sent) == 2 and "Baixou de novo: antes R$ 3.699,00" in env.sent[1]

    with SessionLocal() as db:                               # passadas 24 h, avisa de novo
        for a in db.query(Alert).filter_by(user_id=env.uid):
            a.created_at = a.created_at - timedelta(hours=25)
        db.commit()
    env.msg(-1003, "Nintendo Switch 2 por R$ 3.699")
    assert len(env.sent) == 3
