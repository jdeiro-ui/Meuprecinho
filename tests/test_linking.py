import asyncio

import httpx
import pytest

from price_watch import linking, monitor
from price_watch.db import SessionLocal, init_db
from price_watch.models import Product, Store

ML_URL = "https://produto.mercadolivre.com.br/MLB-5550000005-fone-_JM?matt_tool=123"
SOCIAL = "https://www.mercadolivre.com.br/social/jldeiro?matt_word=jldeiro&matt_tool=1&forceInApp=true&ref=XYZ"
SOCIAL_HTML = ('<html><meta property="og:title" content="Camiseta Tech Light - R$ 79">'
               + "<script>window.__S={" + '"polycards":[{"metadata":{"id":"MLB9990000009"}}],' * 3
               + '"shared_item":{"id":"MLB4232914111","type":"ITEM"},"shared_items":[]}</script></html>')


def _redirects(request: httpx.Request) -> httpx.Response:
    host, path = request.url.host, request.url.path
    if host == "meli.la" and path == "/1KneFL7":
        return httpx.Response(301, headers={"location": SOCIAL})
    if host == "meli.la":
        return httpx.Response(302, headers={"location": ML_URL})
    if path.startswith("/social/"):
        return httpx.Response(200, text=SOCIAL_HTML)
    if host == "bit.ly":  # redirecionamento por HTML
        return httpx.Response(200, text='<meta http-equiv="refresh" content="0;url=https://www.kabum.com.br/produto/1">')
    if host == "amzn.to":
        raise httpx.ConnectError("fora do ar")
    return httpx.Response(200, text="ok")


@pytest.fixture(autouse=True)
def fake_net(monkeypatch):
    monkeypatch.setattr(linking, "transport", httpx.MockTransport(_redirects))


def test_resolve_segue_redirecionamentos():
    assert asyncio.run(linking.resolve("https://meli.la/2AbCdE")) == ML_URL
    assert asyncio.run(linking.resolve("https://bit.ly/x")) == "https://www.kabum.com.br/produto/1"
    assert asyncio.run(linking.resolve("https://amzn.to/x")) == "https://amzn.to/x"   # falhou: mantém
    assert asyncio.run(linking.resolve("https://www.kabum.com.br/p/1")) == "https://www.kabum.com.br/p/1"


def test_produto_antigo_com_link_curto_e_corrigido_e_loja_errada_some():
    init_db()
    with SessionLocal() as db:
        meli = Store(name="Meli", domain="meli.la")
        p = Product(store=meli, url="https://meli.la/2AbCdE", last_error="preço não encontrado na página")
        db.add(p)
        db.commit()
        pid = p.id
    asyncio.run(monitor.fix_short_link(pid))
    with SessionLocal() as db:
        p = db.get(Product, pid)
        assert p.url == ML_URL
        assert p.store.domain == "mercadolivre.com.br" and p.store.fetch_via == "api"
        assert p.last_error is None
        assert db.query(Store).filter_by(domain="meli.la").first() is None


def test_link_meli_la_para_pagina_social_vira_o_produto_compartilhado():
    expected = "https://produto.mercadolivre.com.br/MLB-4232914111"
    assert asyncio.run(linking.resolve("https://meli.la/1KneFL7")) == expected
    assert asyncio.run(linking.resolve(SOCIAL)) == expected  # já gravado como página social
    from price_watch.mercadolivre import parse_ml_id
    assert parse_ml_id(expected) == ("item", "MLB4232914111")
    assert linking.ml_canonical("MLB123456789", "PRODUCT") == "https://www.mercadolivre.com.br/p/MLB123456789"


def test_produto_gravado_com_pagina_social_e_corrigido_e_repetido_avisa():
    init_db()
    with SessionLocal() as db:
        ml = db.query(Store).filter_by(domain="mercadolivre.com.br").first() or \
            Store(name="Mercado Livre", domain="mercadolivre.com.br", fetch_via="api")
        a = Product(store=ml, url=SOCIAL)
        b = Product(store=ml, url=SOCIAL + "2")
        db.add_all([a, b])
        db.commit()
        aid, bid = a.id, b.id
    asyncio.run(monitor.fix_short_link(aid))
    asyncio.run(monitor.fix_short_link(bid))
    with SessionLocal() as db:
        assert db.get(Product, aid).url == "https://produto.mercadolivre.com.br/MLB-4232914111"
        assert "repetido" in db.get(Product, bid).last_error


def test_dados_do_produto_na_pagina_social():
    html = ('<meta property="og:title" content="Camiseta Tech Light - R$ 79,90">'
            '<meta property="og:image" content="https://img/c.webp">'
            '"polycards":[{"metadata":{"id":"MLB111222333"},"price":{"current_price":{"value":10}}},'
            '{"metadata":{"id":"MLB4232914111"},"components":[{"price":{"current_price":{"value":79.9,"currency":"BRL"}}}]}]'
            '"shared_item":{"id":"MLB4232914111","type":"ITEM"}')
    o = linking.ml_social_offer(html)
    assert o == {"id": "MLB4232914111", "price": 7990, "name": "Camiseta Tech Light", "image": "https://img/c.webp"}
    assert linking.ml_social_offer("<html>nada</html>") is None
