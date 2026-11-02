import asyncio
import json
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from price_watch import fetcher, item_rules, monitor, notifier
from price_watch.db import SessionLocal, init_db
from price_watch.models import Item, Product, Rule, Store
from price_watch.pricing import price_in_text
from price_watch.telegram import GroupInfo, TelegramManager


@pytest.mark.parametrize("name, brand, expected", [
    ("Smartphone Samsung Galaxy A27 5G 256GB 8GB RAM | Americanas", "Samsung", "galaxy + a27 + 256gb"),
    ("Kit Starlink Mini X, Antena + Roteador, Internet via Satélite", "STARLINK", "starlink + mini"),
    ("Air Fryer Mondial 4L", "Mondial", "air + fryer + 4l"),
    ("Echo Dot 5ª geração", "Amazon", "echo + dot + 5a"),
    ("Camiseta Tech Light Insider Masculino", None, "camiseta + tech + light"),
    ("Fone", None, None),
    (None, None, None),
])
def test_keyword_for(name, brand, expected):
    assert item_rules.keyword_for(name, brand) == expected


@pytest.mark.parametrize("text, cents", [
    ("De R$ 2.299 por R$ 1.799,90 no Pix", 179990),
    ("Galaxy A27 R$1.849 🔥 cupom", 184900),
    ("por apenas R$ 89,9", 8990),
    ("sem preço aqui", None),
])
def test_price_in_text(text, cents):
    assert price_in_text(text) == cents


def _rule(db, item_id):
    return db.scalar(select(Rule).where(Rule.item_id == item_id))


def test_ciclo_de_vida_da_regra_do_produto():
    init_db()
    with SessionLocal() as db:
        item = Item(name="Air Fryer Mondial 4L")
        db.add(item)
        db.flush()
        assert item_rules.sync(db, item)
        db.commit()
        r = _rule(db, item.id)
        assert (r.keyword, r.auto, r.active) == ("air + fryer + 4l", True, True)
        assert "capinha" in r.exclusions

        item.name = "Air Fryer Mondial 5L"           # nome mudou: palavra acompanha
        assert item_rules.sync(db, item) and _rule(db, item.id).keyword == "air + fryer + 5l"

        r = _rule(db, item.id)
        r.keyword, r.auto = "air fryer + mondial", False  # editada à mão: não sobrescreve
        item.name = "Air Fryer Mondial 6L"
        item_rules.sync(db, item)
        assert _rule(db, item.id).keyword == "air fryer + mondial"

        item.active = False                         # produto pausado pausa a regra
        assert item_rules.sync(db, item) and _rule(db, item.id).active is False

        item.watch_telegram = False                 # desligou o Telegram: regra some
        assert item_rules.sync(db, item)
        db.commit()
        assert _rule(db, item.id) is None

        item.watch_telegram, item.active = True, True
        item_rules.sync(db, item)
        db.commit()
        iid = item.id
        db.delete(item)                              # produto apagado leva a regra junto
        db.commit()
        assert _rule(db, iid) is None


def test_regra_nasce_na_primeira_leitura_e_aviso_do_telegram_compara(monkeypatch):
    init_db()
    monitor.DOMAIN_GAP = 0

    async def fake_fetch(url, mode):
        return ('<script type="application/ld+json">' + json.dumps(
            {"@type": "Product", "name": "Smartphone Samsung Galaxy A27 5G 256GB", "brand": "Samsung",
             "offers": {"price": 1999.0}}) + "</script>")

    sent = []

    async def fake_send(text):
        sent.append(text)
        return True

    monkeypatch.setattr(fetcher, "fetch", fake_fetch)
    monkeypatch.setattr(notifier, "send", fake_send)
    with SessionLocal() as db:
        item = Item(target_cents=185000)
        offer = Product(item=item, store=Store(name="Loja T", domain="loja-t-rules.com.br"),
                        url="https://loja-t-rules.com.br/a27")
        db.add_all([item, offer])
        db.commit()
        iid, oid = item.id, offer.id
    asyncio.run(monitor.check_product(oid))
    with SessionLocal() as db:
        rule = _rule(db, iid)
        assert rule is not None and rule.keyword == "galaxy + a27 + 256gb"

    m = TelegramManager()
    m.reload()
    m.groups = {-100321: GroupInfo("Promos BR", "promosbr")}
    sent.clear()
    ev = SimpleNamespace(chat_id=-100321, id=77, raw_text="🔥 Galaxy A27 256GB de R$ 2.199 por R$ 1.799 no Pix!")
    asyncio.run(m._on_message(ev))
    assert len(sent) == 1
    msg = sent[0]
    assert "Seu produto" in msg and "Galaxy A27" in msg
    assert "R$ 1.799,00" in msg and "abaixo do seu alvo" in msg and "melhor que nas lojas" in msg
