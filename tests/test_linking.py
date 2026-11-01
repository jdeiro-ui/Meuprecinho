import asyncio

import httpx
import pytest

from price_watch import linking, monitor
from price_watch.db import SessionLocal, init_db
from price_watch.models import Product, Store

ML_URL = "https://produto.mercadolivre.com.br/MLB-5550000005-fone-_JM?matt_tool=123"


def _redirects(request: httpx.Request) -> httpx.Response:
    host, path = request.url.host, request.url.path
    if host == "meli.la":
        return httpx.Response(302, headers={"location": ML_URL})
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
