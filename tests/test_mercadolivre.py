import asyncio
import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from price_watch import main, mercadolivre, monitor, notifier
from price_watch.config import get_settings
from price_watch.db import SessionLocal, init_db
from price_watch.fetcher import FetchError
from price_watch.models import Product, Store


@pytest.mark.parametrize("url, expected", [
    ("https://produto.mercadolivre.com.br/MLB-1234567890-fone-bluetooth-_JM", ("item", "MLB1234567890")),
    ("https://www.mercadolivre.com.br/fone-xyz/p/MLB19615315", ("product", "MLB19615315")),
    ("https://www.mercadolivre.com.br/fone/p/MLB19615315?pdp_filters=item_id:MLB4156342870", ("item", "MLB4156342870")),
    ("https://www.mercadolivre.com.br/fone/p/MLB19615315#wid=MLB999888777", ("item", "MLB999888777")),
    ("https://www.mercadolivre.com.br/ofertas", None),
])
def test_parse_ml_id(url, expected):
    assert mercadolivre.parse_ml_id(url) == expected


class FakeML:
    """API falsa: registra chamadas e responde como o Mercado Livre."""

    def __init__(self):
        self.calls = []
        self.refreshes = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        self.calls.append(path)
        if path == "/oauth/token":
            form = parse_qs(request.content.decode())
            if form["grant_type"] == ["refresh_token"]:
                self.refreshes += 1
            return httpx.Response(200, json={"access_token": f"AT{len(self.calls)}", "expires_in": 21600,
                                             "refresh_token": f"RT{len(self.calls)}"})
        if request.headers.get("authorization", "").startswith("Bearer AT") is False:
            return httpx.Response(401, json={})
        if path == "/users/me":
            return httpx.Response(200, json={"nickname": "JDEIRO"})
        if path == "/items/MLB1110000001":
            return httpx.Response(200, json={"title": "Fone XYZ", "price": 199.9, "status": "active",
                                             "available_quantity": 5,
                                             "pictures": [{"secure_url": "https://img/f.jpg"}]})
        if path == "/items/MLB4040000004":
            return httpx.Response(404, json={})
        if path == "/products/MLB22200002":
            return httpx.Response(200, json={"name": "Echo Dot 5", "buy_box_winner": None,
                                             "pictures": [{"url": "https://img/e.jpg"}], "status": "active"})
        if path == "/products/MLB22200002/items":
            return httpx.Response(200, json={"results": [{"price": 389.0}, {"price": 359.9}]})
        return httpx.Response(404, json={})


@pytest.fixture
def ml(monkeypatch):
    init_db()
    s = get_settings()
    monkeypatch.setattr(s, "ML_CLIENT_ID", "123")
    monkeypatch.setattr(s, "ML_CLIENT_SECRET", "segredo")
    fake = FakeML()
    monkeypatch.setattr(mercadolivre, "transport", httpx.MockTransport(fake.handler))
    mercadolivre.disconnect()
    return fake


def test_sem_conta_conectada_da_erro_claro(ml):
    with pytest.raises(FetchError, match="conecte sua conta"):
        asyncio.run(mercadolivre.fetch_price("https://produto.mercadolivre.com.br/MLB-1110000001-x"))


def test_fluxo_oauth_completo_pelo_painel(ml):
    c = TestClient(main.app)
    with SessionLocal() as db:
        from price_watch.models import User
        from price_watch.security import hash_password
        if not db.query(User).filter_by(username="mladmin").first():
            db.add(User(username="mladmin", password_hash=hash_password("senha-ml-123")))
            db.commit()
    main._fails.clear()
    c.post("/login", data={"username": "mladmin", "password": "senha-ml-123"})

    r = c.get("/ml/connect", follow_redirects=False)
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert r.headers["location"].startswith(mercadolivre.AUTH_URL)
    assert q["client_id"] == ["123"] and q["code_challenge_method"] == ["S256"]
    assert q["redirect_uri"][0].endswith("/ml/callback")

    bad = c.get("/ml/callback?code=abc&state=errado", follow_redirects=False)
    assert mercadolivre.account() is None and bad.status_code == 303

    c.get("/ml/connect", follow_redirects=False)  # novo state (o anterior foi consumido)
    state = c.get("/ml/connect", follow_redirects=False).headers["location"]
    state = parse_qs(urlsplit(state).query)["state"][0]
    c.get(f"/ml/callback?code=abc&state={state}", follow_redirects=False)
    assert mercadolivre.account() == "JDEIRO"
    page = c.get("/telegram").text
    assert "conectado como <b>JDEIRO</b>" in page and "segredo" not in page


def test_preco_de_anuncio_e_de_catalogo_e_renovacao_do_token(ml):
    mercadolivre._put({"ml_refresh_token": "RT0", "ml_access_token": "AT0", "ml_expires_at": int(time.time()) + 999})
    ex = asyncio.run(mercadolivre.fetch_price("https://produto.mercadolivre.com.br/MLB-1110000001-fone-_JM"))
    assert (ex.price, ex.name, ex.image, ex.available) == (19990, "Fone XYZ", "https://img/f.jpg", True)
    assert ml.refreshes == 0

    ex = asyncio.run(mercadolivre.fetch_price("https://www.mercadolivre.com.br/echo/p/MLB22200002"))
    assert ex.price == 35990 and ex.name == "Echo Dot 5"  # menor preço entre os anúncios

    mercadolivre._put({"ml_expires_at": 0})  # token vencido: renova sozinho
    asyncio.run(mercadolivre.fetch_price("https://produto.mercadolivre.com.br/MLB-1110000001-fone-_JM"))
    assert ml.refreshes == 1
    assert mercadolivre._get(("ml_refresh_token",))["ml_refresh_token"].startswith("RT")

    with pytest.raises(FetchError, match="não encontrado"):
        asyncio.run(mercadolivre.fetch_price("https://produto.mercadolivre.com.br/MLB-4040000004-x"))


def test_monitor_usa_a_api_para_lojas_api(ml, monkeypatch):
    mercadolivre._put({"ml_refresh_token": "RT0", "ml_access_token": "AT0", "ml_expires_at": int(time.time()) + 999})
    sent = []

    async def fake_send(text):
        sent.append(text)
        return True

    monkeypatch.setattr(notifier, "send", fake_send)
    with SessionLocal() as db:
        store = db.query(Store).filter_by(domain="mercadolivre.com.br").first() or \
            Store(name="Mercado Livre", domain="mercadolivre.com.br", fetch_via="api")
        p = Product(store=store, url="https://produto.mercadolivre.com.br/MLB-1110000001-fone-_JM-teste",
                    target_cents=20000)
        db.add(p)
        db.commit()
        pid = p.id
    assert pid in monitor.due_product_ids("api")
    asyncio.run(monitor.check_product(pid))
    with SessionLocal() as db:
        p = db.get(Product, pid)
        assert p.current_cents == 19990 and p.last_error is None
    assert len(sent) == 1


def test_erro_de_autorizacao_mostra_motivo_e_tenta_sem_pkce(ml, monkeypatch):
    seen = []

    def handler(request):
        form = parse_qs(request.content.decode())
        seen.append("code_verifier" in form)
        if request.url.path == "/oauth/token" and "code_verifier" in form:
            return httpx.Response(400, json={"error": "invalid_grant", "message": "code_verifier inválido"})
        if request.url.path == "/oauth/token":
            return httpx.Response(200, json={"access_token": "AT1", "refresh_token": "RT1", "expires_in": 21600})
        return httpx.Response(200, json={"nickname": "SEMPKCE"})

    monkeypatch.setattr(mercadolivre, "transport", httpx.MockTransport(handler))
    asyncio.run(mercadolivre.exchange_code("abc", "https://x/ml/callback", "verif"))
    assert seen[:2] == [True, False] and mercadolivre.account() == "SEMPKCE"

    def always_bad(request):
        return httpx.Response(400, json={"error": "invalid_client", "message": "chave errada: segredo"})

    monkeypatch.setattr(mercadolivre, "transport", httpx.MockTransport(always_bad))
    with pytest.raises(FetchError) as exc:
        asyncio.run(mercadolivre.exchange_code("abc", "https://x/ml/callback", "verif"))
    assert "invalid_client" in str(exc.value) and "chave errada: ***" in str(exc.value)


def test_conexao_sem_offline_access_aparece_e_avisa(ml, monkeypatch):
    def handler(request):
        if request.url.path == "/oauth/token":  # sem refresh_token (falta offline_access)
            return httpx.Response(200, json={"access_token": "AT9", "expires_in": 21600})
        return httpx.Response(200, json={"nickname": "SEMOFFLINE"})

    monkeypatch.setattr(mercadolivre, "transport", httpx.MockTransport(handler))
    asyncio.run(mercadolivre.exchange_code("abc", "https://x/ml/callback", "v"))
    st = mercadolivre.status()
    assert st["connected"] and not st["renewable"] and mercadolivre.account() == "SEMOFFLINE"
    assert asyncio.run(mercadolivre._access_token()) == "AT9"   # funciona enquanto o token vale

    mercadolivre._put({"ml_expires_at": 0})
    assert mercadolivre.status()["expired"]
    with pytest.raises(FetchError, match="offline_access"):
        asyncio.run(mercadolivre._access_token())
