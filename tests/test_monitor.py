import asyncio
import json

from sqlalchemy import select

from price_watch import fetcher, monitor, notifier
from price_watch.db import SessionLocal, init_db
from price_watch.models import Alert, PricePoint, Product, Store


def _page(price):
    return ('<script type="application/ld+json">'
            + json.dumps({"@type": "Product", "name": "Fone Bluetooth", "offers": {"price": price}})
            + "</script>")


def test_monitor_le_preco_avisa_queda_e_registra_erros(monkeypatch):
    init_db()
    monitor.DOMAIN_GAP = 0
    prices = iter([200.0, 180.0, 180.0, "erro"])
    sent = []

    async def fake_fetch(url, mode):
        p = next(prices)
        if p == "erro":
            raise fetcher.FetchError("a loja bloqueou a leitura (HTTP 403)")
        return _page(p)

    async def fake_send(text, **kw):
        sent.append(text)
        return True

    monkeypatch.setattr(fetcher, "fetch", fake_fetch)
    monkeypatch.setattr(notifier, "send", fake_send)

    with SessionLocal() as db:
        store = Store(name="Loja Teste", domain="lojamonitor.com.br")
        p = Product(store=store, url="https://lojamonitor.com.br/fone")
        db.add(p)
        db.commit()
        pid = p.id

    assert pid in monitor.due_product_ids()
    for _ in range(4):
        asyncio.run(monitor.check_product(pid))

    with SessionLocal() as db:
        p = db.get(Product, pid)
        assert p.name == "Fone Bluetooth"
        assert p.current_cents == 18000 and p.lowest_cents == 18000
        assert p.last_error == "a loja bloqueou a leitura (HTTP 403)"
        points = db.scalars(select(PricePoint).where(PricePoint.product_id == pid)).all()
        assert [pt.cents for pt in points] == [20000, 18000]  # preço repetido não duplica
        alerts = db.scalars(select(Alert).where(Alert.link == p.url)).all()
    assert pid not in monitor.due_product_ids()  # acabou de ser consultado
    assert len(sent) == 1 and len(alerts) == 1  # só a queda gerou aviso
    assert "R$ 200,00" in sent[0] and "R$ 180,00" in sent[0] and "menor preço" in sent[0]
    assert alerts[0].source == "price"


def test_erro_inesperado_na_leitura_vira_erro_na_tela(monkeypatch):
    init_db()
    monitor.DOMAIN_GAP = 0

    async def broken_fetch(url, mode):
        raise RuntimeError("falha qualquer")

    monkeypatch.setattr(fetcher, "fetch", broken_fetch)
    with SessionLocal() as db:
        p = Product(store=Store(name="Quebra", domain="quebra.com.br"), url="https://quebra.com.br/x")
        db.add(p)
        db.commit()
        pid = p.id
    asyncio.run(monitor.check_product(pid))  # não pode levantar exceção
    with SessionLocal() as db:
        assert db.get(Product, pid).last_error == "erro inesperado na leitura (RuntimeError)"
