import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from price_watch import discovery, notifier, zoom
from price_watch.db import SessionLocal, init_db
from price_watch.item_rules import keyword_for, sync
from price_watch.models import Item, Product, Store
from price_watch.pricing import pack_qty
from price_watch.telegram import GroupInfo, TelegramManager


@pytest.mark.parametrize("text, n", [
    ("Barra Wafer Bold Chocolate ao Leite 40g Caixa com 12 unidades", 12),
    ("BARRA WAFER CHOCOLATE AO LEITE 40G BOLD", 1),
    ("Bold Thin Cookies 40g c/12", 12),
    ("Kit 12 Barras de Proteína Bold 60g", 12),
    ("Bold Wafer 12x40g", 12),
    ("Bold Wafer 40g x 12", 12),
    ("Display 12 un Bold Tube", 12),
    ("Pack com 6 latas Heineken 350ml", 6),
    ("Galaxy A27 em 12x de R$ 166,58", 1),   # parcelamento não é quantidade
    ("Caixa de som JBL", 1),
    ("Leve 3 pague 2", 1),
])
def test_pack_qty(text, n):
    assert pack_qty(text) == n


def test_quantidade_nao_entra_nas_palavras():
    box = keyword_for("Barra Wafer Bold Chocolate ao Leite 40g Caixa com 12 unidades", "Bold")
    assert box == keyword_for("Barra Wafer Bold Chocolate ao Leite 40g", "Bold")  # conferida à parte
    assert "12" not in box.split(" + ")


def test_busca_nao_confunde_caixa_com_unidade():
    box = Item(name="Barra Wafer Bold Chocolate ao Leite 40g Caixa com 12 unidades", brand="Bold")
    single = Item(name="Barra Wafer Bold Chocolate ao Leite 40g", brand="Bold")
    C = discovery.Candidate
    loose = C("Americanas", "u", "BARRA WAFER CHOCOLATE AO LEITE 40G BOLD")
    boxed = C("Americanas", "u", "Barra Wafer Chocolate ao Leite Bold 40g - Caixa c/ 12")
    assert discovery.match_kind(box, loose) is None and discovery.match_kind(box, boxed) == "nome"
    assert discovery.match_kind(single, loose) == "nome" and discovery.match_kind(single, boxed) is None


def _next(state):
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps({"props": {"initialReduxState": state}})}</script>'


def test_zoom_so_conta_ofertas_com_a_mesma_quantidade(monkeypatch):
    init_db()
    page = _next({"offers": {"displayOffers": [
        {"id": "1", "name": "Barra Wafer Bold Chocolate ao Leite 40g", "price": 9.99, "sellerName": "Loja Avulsa"},
        {"id": "2", "name": "Bold Wafer Chocolate ao Leite 40g Caixa com 12", "price": 99.9, "sellerName": "Loja Caixa"},
    ]}})
    monkeypatch.setattr(zoom, "transport", httpx.MockTransport(lambda r: httpx.Response(200, text=page)))
    with SessionLocal() as db:
        item = Item(name="Barra Wafer Bold Chocolate ao Leite 40g Caixa com 12 unidades", brand="Bold",
                    zoom_url="https://www.zoom.com.br/suplementos/bold-wafer-teste")
        db.add(item)
        db.commit()
        iid = item.id
    results = asyncio.run(zoom.refresh(iid))
    assert [ex.price for _, ex in results] == [9990]   # só a caixa; a unidade de R$ 9,99 ficou de fora


def test_telegram_promocao_da_unidade_nao_avisa_a_caixa(monkeypatch):
    init_db()
    sent = []

    async def fake_send(text):
        sent.append(text)
        return True

    monkeypatch.setattr(notifier, "send", fake_send)
    with SessionLocal() as db:
        item = Item(name="Barra Wafer Bold Pistache 40g Caixa com 12 unidades", brand="Bold")
        db.add_all([item, Product(item=item, store=Store(name="Loja P", domain="loja-p-pack.com.br"),
                                  url="https://loja-p-pack.com.br/x")])
        db.flush()
        sync(db, item)
        db.commit()
    m = TelegramManager()
    m.reload()
    m.groups = {-100555: GroupInfo("Promos", None)}
    asyncio.run(m._on_message(SimpleNamespace(chat_id=-100555, id=1, raw_text="Bold Wafer Pistache 40g por R$ 8,99")))
    assert sent == []
    asyncio.run(m._on_message(SimpleNamespace(chat_id=-100555, id=2,
                                              raw_text="Caixa Bold Wafer Pistache 40g com 12 unidades R$ 89,90")))
    assert len(sent) == 1 and "Seu produto" in sent[0]
