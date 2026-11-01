import gzip
import json

from fastapi.testclient import TestClient

from price_watch import main, monitor, notifier
from price_watch.db import SessionLocal, init_db
from price_watch.models import Product, Store
from price_watch.routes_collector import collector_token


def _page(price):
    return ('<script type="application/ld+json">'
            + json.dumps({"@type": "Product", "name": "Echo Dot", "offers": {"price": price}})
            + "</script>").encode()


def test_coletor_fila_reserva_e_resultado(monkeypatch):
    init_db()
    sent = []

    async def fake_send(text):
        sent.append(text)
        return True

    monkeypatch.setattr(notifier, "send", fake_send)
    monitor._leases.clear()
    with SessionLocal() as db:
        home = Store(name="Amazon", domain="amazon-teste.com.br", fetch_via="home")
        vm = Store(name="KaBuM", domain="kabum-teste.com.br", fetch_via="vm")
        a = Product(store=home, url="https://amazon-teste.com.br/dp/1", target_cents=40000)
        b = Product(store=vm, url="https://kabum-teste.com.br/p/1")
        db.add_all([a, b])
        db.commit()
        aid, bid = a.id, b.id

    c = TestClient(main.app)
    assert c.get("/api/coletor/jobs").status_code == 401
    assert c.get("/api/coletor/jobs", headers={"Authorization": "Bearer errado"}).status_code == 401

    auth = {"Authorization": f"Bearer {collector_token()}"}
    ids = [j["id"] for j in c.get("/api/coletor/jobs", headers=auth).json()["jobs"]]
    assert aid in ids and bid not in ids                      # só lojas "home"
    assert aid not in [j["id"] for j in c.get("/api/coletor/jobs", headers=auth).json()["jobs"]]  # reservado
    assert bid in monitor.due_product_ids("vm") and aid not in monitor.due_product_ids("vm")

    r = c.post(f"/api/coletor/result?id={aid}&status=200", headers=auth, content=gzip.compress(_page(379.0)))
    assert r.json() == {"ok": True}
    with SessionLocal() as db:
        p = db.get(Product, aid)
        assert p.current_cents == 37900 and p.name == "Echo Dot" and p.last_error is None
    assert len(sent) == 1 and "abaixo do seu preço-alvo" in sent[0]

    # loja bloqueou: vira erro na tela, sem aviso
    c.post(f"/api/coletor/result?id={aid}&status=403", headers=auth, content=gzip.compress(b"denied"))
    with SessionLocal() as db:
        assert "bloqueou" in db.get(Product, aid).last_error

    # erro do curl no celular
    c.post(f"/api/coletor/result?id={aid}&status=0&error=sem+resposta+da+loja", headers=auth, content=b"")
    with SessionLocal() as db:
        assert db.get(Product, aid).last_error == "sem resposta da loja"

    # "Consultar" recoloca na fila na hora
    monitor.request_now(aid)
    assert aid in [j["id"] for j in c.get("/api/coletor/jobs", headers=auth).json()["jobs"]]
    assert monitor.collector_silent_for().total_seconds() < 5


def test_token_novo_invalida_o_antigo():
    init_db()
    old = collector_token()
    new = collector_token(regenerate=True)
    assert old != new and collector_token() == new
    c = TestClient(main.app)
    assert c.get("/api/coletor/jobs", headers={"Authorization": f"Bearer {old}"}).status_code == 401


def test_script_do_coletor_e_publico_e_sem_segredos():
    c = TestClient(main.app)
    r = c.get("/coletor.py")
    assert r.status_code == 200 and "def main()" in r.text
    assert collector_token() not in r.text
