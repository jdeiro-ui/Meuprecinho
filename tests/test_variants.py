import asyncio
from types import SimpleNamespace

import pytest

from price_watch import discovery, notifier
from price_watch.db import SessionLocal, init_db
from price_watch.item_rules import sync, variant_conflict, variant_signature
from price_watch.models import Item, Product, Store
from price_watch.telegram import GroupInfo, TelegramManager


@pytest.mark.parametrize("name, brand, text, conflict", [
    ("Console Nintendo Switch 32GB", "Nintendo", "Console Nintendo Switch 2 256GB", True),
    ("Console Nintendo Switch 32GB", "Nintendo", "Nintendo Switch Lite Azul", True),
    ("Console Nintendo Switch 32GB", "Nintendo", "Nintendo Switch OLED Branco", True),
    ("Console Nintendo Switch 32GB", "Nintendo", "Console Nintendo Switch 32GB Neon", False),
    ("Console Nintendo Switch 2", "Nintendo", "Nintendo Switch 2 + Mario Kart World", False),
    ("Console Nintendo Switch 2", "Nintendo", "Console Nintendo Switch 32GB", True),
    ("Apple iPhone 17 256GB Preto", "Apple", "iPhone 17 256GB Azul", False),
    ("Apple iPhone 17 256GB Preto", "Apple", "iPhone 17 Pro 256GB", True),
    ("Apple iPhone 17 256GB Preto", "Apple", "Apple iPhone 16 256GB", True),
    ("Smartphone Samsung Galaxy S24 256GB", "Samsung", "Galaxy S24 Ultra 256GB", True),
    ("Kindle 16 GB (Geração 2024)", None, "Kindle Paperwhite 16GB", True),
    ("Console PlayStation 5 Slim Digital", "Sony", "PlayStation 5 Pro", True),
    ("Console PlayStation 5 Slim Digital", "Sony", "PlayStation 5 Slim Digital 1TB", False),
])
def test_conferencia_de_versao(name, brand, text, conflict):
    assert variant_conflict(variant_signature(name, brand), text) is conflict


def test_busca_nas_lojas_nao_aceita_outra_versao():
    item = Item(name="Console Nintendo Switch 32GB", brand="Nintendo")
    C = discovery.Candidate
    assert discovery.match_kind(item, C("X", "u", "Console Nintendo Switch 2 256GB")) is None
    assert discovery.match_kind(item, C("X", "u", "Console Nintendo Switch 32GB Neon")) == "nome"


def test_telegram_promocao_do_switch_2_nao_avisa_o_switch(monkeypatch):
    init_db()
    sent = []

    async def fake_send(text, **kw):
        sent.append(text)
        return True

    monkeypatch.setattr(notifier, "send", fake_send)
    with SessionLocal() as db:
        item = Item(name="Console Nintendo Switch 32GB", brand="Nintendo")
        db.add_all([item, Product(item=item, store=Store(name="Loja S", domain="loja-s-var.com.br"),
                                  url="https://loja-s-var.com.br/switch")])
        db.flush()
        sync(db, item)
        db.commit()
    m = TelegramManager()
    m.reload()
    m.groups = {-100777: GroupInfo("Games", None)}
    asyncio.run(m._on_message(SimpleNamespace(chat_id=-100777, id=1, raw_text="Nintendo Switch 2 por R$ 3.999 no Pix")))
    assert sent == []
    asyncio.run(m._on_message(SimpleNamespace(chat_id=-100777, id=2, raw_text="Nintendo Switch 32GB por R$ 1.799")))
    assert len(sent) == 1
