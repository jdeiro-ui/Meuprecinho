import asyncio
import json

import httpx
import pytest

from price_watch import monitor, notifier, zoom
from price_watch.db import SessionLocal, init_db
from price_watch.models import Item, Product, Store


def _next(state):
    return f'<script id="__NEXT_DATA__" type="application/json">{json.dumps({"props": {"initialReduxState": state}})}</script>'


SEARCH = _next({"hits": {"hits": [
    {"type": "product", "name": "Celular Apple iPhone 17 256GB", "url": "/celular/celular-apple-iphone-17-256gb", "price": 5699},
    {"type": "product", "name": "Celular Apple iPhone 17 Pro 256GB", "url": "/celular/iphone-17-pro", "price": 7999},
    {"type": "offer", "name": "Apple iPhone 17 256GB (Seminovo)", "url": "/lead?oid=1", "price": 3999,
     "merchantName": "Loja X", "bestOffer": {"id": "1"}},
]}})
PRODUCT = _next({"offers": {"displayOffers": [
    {"id": "11", "name": "Apple iPhone 17 256GB Preto", "price": 5699, "sellerName": "Magazine Luiza"},
    {"id": "12", "name": "Apple iPhone 17 256GB Branco", "price": 5799, "sellerName": "Magazine Luiza"},
    {"id": "13", "name": "Apple iPhone 17 256GB (Seminovo)", "price": 4100, "sellerName": "Amazon"},
    {"id": "14", "name": "Apple iPhone 17 256GB", "price": 5899, "sellerName": "KaBuM!"},
    {"id": "15", "name": "Apple iPhone 17 256GB", "price": 5650, "sellerName": "Webcontinental"},
]}})


def test_parse_search_e_produto():
    products, offers = zoom.parse_search(SEARCH)
    assert [p["url"] for p in products] == ["https://www.zoom.com.br/celular/celular-apple-iphone-17-256gb",
                                             "https://www.zoom.com.br/celular/iphone-17-pro"]
    assert offers == []  # o seminovo ficou de fora
    best = zoom.best_per_seller(zoom.parse_product(PRODUCT))
    assert [(o.seller, o.price) for o in best] == [("Webcontinental", 565000), ("Magazine Luiza", 569900),
                                                   ("KaBuM!", 589900)]


@pytest.fixture
def zoom_net(monkeypatch):
    pages = {"/search": SEARCH, "/celular/celular-apple-iphone-17-256gb": PRODUCT}
    monkeypatch.setattr(zoom, "transport", httpx.MockTransport(
        lambda r: httpx.Response(200, text=pages.get(r.url.path, "")) if r.url.path in pages else httpx.Response(404)))


def test_find_escolhe_o_produto_certo(zoom_net):
    url, note = asyncio.run(zoom.find(Item(name="Apple iPhone 17 256GB Preto", brand="Apple")))
    assert url == "https://www.zoom.com.br/celular/celular-apple-iphone-17-256gb" and "encontrado" in note


def test_ofertas_do_zoom_entram_na_comparacao_e_avisam(zoom_net, monkeypatch):
    init_db()
    sent = []

    async def fake_send(text, **kw):
        sent.append(text)
        return True

    monkeypatch.setattr(notifier, "send", fake_send)
    with SessionLocal() as db:
        kabum = db.query(Store).filter_by(domain="kabum.com.br").first() or Store(name="KaBuM!", domain="kabum.com.br")
        item = Item(name="Apple iPhone 17 256GB", brand="Apple", target_cents=570000,
                    zoom_url="https://www.zoom.com.br/celular/celular-apple-iphone-17-256gb")
        direct = Product(item=item, store=kabum, url="https://www.kabum.com.br/produto/zoomtest",
                         current_cents=600000, available=True)
        db.add_all([item, direct])
        db.commit()
        iid = item.id

    n = asyncio.run(monitor.refresh_zoom(iid))
    assert n == 2   # Webcontinental e Magalu; KaBuM já é lido direto
    with SessionLocal() as db:
        item = db.get(Item, iid)
        zoom_offers = sorted((o.store.name, o.current_cents) for o in item.offers if o.zoom_oid)
        assert zoom_offers == [("Magazine Luiza", 569900), ("Webcontinental", 565000)]
        assert item.best_cents == 565000 and item.best_offer().store.name == "Webcontinental"
        assert db.query(Store).filter_by(domain="magazineluiza.com.br").count() == 1
    assert len(sent) == 1 and "Webcontinental" in sent[0] and "abaixo do seu preço-alvo" in sent[0]

    asyncio.run(monitor.refresh_zoom(iid))  # de novo: não duplica nem repete aviso
    with SessionLocal() as db:
        assert len([o for o in db.get(Item, iid).offers if o.zoom_oid]) == 2
    assert len(sent) == 1
