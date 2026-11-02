import asyncio
import json

import httpx
import pytest

from price_watch import discovery, monitor
from price_watch.db import SessionLocal, init_db
from price_watch.models import Item, Product, Store, Suggestion

VTEX = [
    {"productName": "Smartphone Samsung Galaxy A27 5G 256GB Preto", "link": "https://www.americanas.com.br/galaxy-a27-preto/p",
     "items": [{"ean": "7892509150538", "sellers": [{"commertialOffer": {"Price": 1999.0, "AvailableQuantity": 10}}]}]},
]
KABUM_HTML = ('<script id="__NEXT_DATA__" type="application/json">' + json.dumps({"props": {"pageProps": {"data": {
    "catalog": [{"code": 900001, "friendlyName": "galaxy-a27-256gb", "name": "Celular Samsung Galaxy A27 256GB 8GB",
                 "priceWithDiscount": 1899.9},
                {"code": 900002, "friendlyName": "capa-galaxy", "name": "Capa para Galaxy S24", "price": 49.9}]}}}})
              + "</script>")


def _net(request: httpx.Request) -> httpx.Response:
    if request.url.host == "www.americanas.com.br":
        ean = "alternateIds_Ean:7892509150538" in str(request.url.params.get("fq"))
        return httpx.Response(206, json=VTEX if ean else [])
    if request.url.host == "www.kabum.com.br":
        return httpx.Response(200, text=KABUM_HTML)
    return httpx.Response(404)


@pytest.fixture
def lojas(monkeypatch):
    monkeypatch.setattr(discovery, "transport", httpx.MockTransport(_net))
    monkeypatch.setattr(discovery, "SEARCHERS", {"americanas.com.br": discovery.search_americanas,
                                                 "kabum.com.br": discovery.search_kabum})


def test_match_kind():
    item = Item(name="Smartphone Samsung Galaxy A27 5G 256GB", brand="Samsung", model="SM-A276B", gtin="7892509150538")
    C = discovery.Candidate
    assert discovery.match_kind(item, C("X", "u", "qualquer", gtin="07892509150538")) == "ean"
    assert discovery.match_kind(item, C("X", "u", "Galaxy A27 SM A276B 256GB")) == "modelo"
    assert discovery.match_kind(item, C("X", "u", "Celular Galaxy A27 256GB 8GB")) == "nome"
    assert discovery.match_kind(item, C("X", "u", "Galaxy A27 128GB")) is None


def test_busca_adiciona_por_ean_e_sugere_por_nome(lojas, monkeypatch):
    init_db()
    checked = []

    async def fake_check(pid):
        checked.append(pid)

    monkeypatch.setattr(monitor, "check_product", fake_check)
    with SessionLocal() as db:
        item = Item(name="Smartphone Samsung Galaxy A27 5G 256GB", brand="Samsung", gtin="7892509150538")
        db.add_all([item, Product(item=item, store=Store(name="Loja D", domain="loja-d-disc.com.br"),
                                  url="https://loja-d-disc.com.br/a27")])
        db.commit()
        iid = item.id

    summary = asyncio.run(monitor.run_discovery(iid))
    assert summary == {"added": ["Americanas"], "suggested": 1}
    with SessionLocal() as db:
        item = db.get(Item, iid)
        assert item.discovered_at is not None
        assert sorted(o.store.domain for o in item.offers) == ["americanas.com.br", "loja-d-disc.com.br"]
        sugs = db.query(Suggestion).filter_by(item_id=iid).all()
        assert [s.title for s in sugs] == ["Celular Samsung Galaxy A27 256GB 8GB"]   # a capa ficou de fora
        assert sugs[0].price_cents == 189990 and sugs[0].url.startswith("https://www.kabum.com.br/produto/900001")
    assert len(checked) == 1   # a oferta nova da Americanas já foi consultada

    # segunda busca não duplica nada
    assert asyncio.run(monitor.run_discovery(iid)) == {"added": [], "suggested": 0}


def test_tela_mostra_sugestoes_e_confirmar_vira_oferta(lojas, monkeypatch):
    from fastapi.testclient import TestClient

    from price_watch import main
    from price_watch.models import User
    from price_watch.security import hash_password

    init_db()

    async def fake_check(pid):
        return None

    monkeypatch.setattr(monitor, "check_product", fake_check)
    with SessionLocal() as db:
        if not db.query(User).filter_by(username="disc").first():
            db.add(User(username="disc", password_hash=hash_password("senha-disc-1")))
        item = Item(name="Smartphone Samsung Galaxy A27 5G 256GB", brand="Samsung")
        db.add_all([item, Product(item=item, store=Store(name="Loja E", domain="loja-e-disc.com.br"),
                                  url="https://loja-e-disc.com.br/a27"),
                    ])
        db.commit()
        iid = item.id
    main._fails.clear()
    c = TestClient(main.app)
    c.post("/login", data={"username": "disc", "password": "senha-disc-1"})
    c.post(f"/products/{iid}/discover")
    page = c.get(f"/products/{iid}").text
    assert "Também encontramos" in page and "Celular Samsung Galaxy A27 256GB 8GB" in page
    with SessionLocal() as db:
        sid = db.query(Suggestion).filter_by(item_id=iid).first().id
    c.post(f"/suggestions/{sid}/accept")
    with SessionLocal() as db:
        assert any(o.store.domain == "kabum.com.br" for o in db.get(Item, iid).offers)
        assert db.query(Suggestion).filter_by(item_id=iid).count() == 0
