import asyncio
import json
import sqlite3

from fastapi.testclient import TestClient

from price_watch import fetcher, main, monitor, notifier
from price_watch.db import SessionLocal, init_db
from price_watch.models import Item, Product, Store
from price_watch.pricing import extract


def _page(price, gtin="7891234567895"):
    return ('<script type="application/ld+json">' + json.dumps({
        "@type": "Product", "name": "TV Samsung 55 QN90", "gtin13": gtin, "mpn": "QN55QN90",
        "brand": {"@type": "Brand", "name": "Samsung"}, "offers": {"price": price}}) + "</script>")


def test_extrai_ean_marca_e_modelo():
    ex = extract(_page(3999.0, gtin="07891234567895"), "https://x.com.br/tv")
    assert (ex.gtin, ex.brand, ex.model) == ("7891234567895", "Samsung", "QN55QN90")


def test_melhor_preco_entre_lojas_e_aviso_aponta_a_loja(monkeypatch):
    init_db()
    monitor.DOMAIN_GAP = 0
    prices = {"https://loja-a-multi.com.br/tv": 4200.0, "https://loja-b-multi.com.br/tv": 3899.0}
    sent = []

    async def fake_fetch(url, mode):
        return _page(prices[url])

    async def fake_send(text, **kw):
        sent.append(text)
        return True

    monkeypatch.setattr(fetcher, "fetch", fake_fetch)
    monkeypatch.setattr(notifier, "send", fake_send)
    with SessionLocal() as db:
        item = Item(name="Minha TV", target_cents=400000)
        a = Product(item=item, store=Store(name="Loja A", domain="loja-a-multi.com.br"), url="https://loja-a-multi.com.br/tv")
        b = Product(item=item, store=Store(name="Loja B", domain="loja-b-multi.com.br"), url="https://loja-b-multi.com.br/tv")
        db.add_all([item, a, b])
        db.commit()
        iid, aid, bid = item.id, a.id, b.id

    asyncio.run(monitor.check_product(aid))   # 4.200 > alvo: sem aviso
    assert sent == []
    asyncio.run(monitor.check_product(bid))   # 3.899 na Loja B: avisa
    assert len(sent) == 1 and "Loja B" in sent[0] and "R$ 3.899,00" in sent[0]
    asyncio.run(monitor.check_product(aid))   # nada mudou no melhor preço: não repete
    assert len(sent) == 1

    with SessionLocal() as db:
        item = db.get(Item, iid)
        assert item.best_cents == 389900 and item.best_offer().id == bid
        assert item.gtin == "7891234567895" and item.brand == "Samsung"

    prices["https://loja-a-multi.com.br/tv"] = 3799.0  # outra loja fica ainda mais barata: avisa de novo
    asyncio.run(monitor.check_product(aid))
    assert len(sent) == 2 and "Loja A" in sent[1]


def test_adicionar_outra_loja_pelo_painel(monkeypatch):
    init_db()

    async def no_check(pid):
        return None

    monkeypatch.setattr(monitor, "check_product", no_check)
    with SessionLocal() as db:
        from price_watch.models import User
        from price_watch.security import hash_password
        user = db.query(User).filter_by(username="multi").first()
        if not user:
            user = User(username="multi", password_hash=hash_password("senha-multi-1"))
            db.add(user)
            db.flush()
        item = Item(name="Fone", user_id=user.id)
        db.add_all([item, Product(item=item, store=Store(name="Loja C", domain="loja-c-multi.com.br"),
                                  url="https://loja-c-multi.com.br/fone")])
        db.commit()
        iid = item.id
    main._fails.clear()
    c = TestClient(main.app)
    c.post("/login", data={"username": "multi", "password": "senha-multi-1"})
    c.post(f"/products/{iid}/offers", data={"url": "Veja https://www.kabum.com.br/produto/555?utm_source=grupo"})
    page = c.get(f"/products/{iid}").text
    assert "Preço por loja" in page and "KaBuM" in page and "Loja C" in page
    with SessionLocal() as db:
        urls = sorted(o.url for o in db.get(Item, iid).offers)
    assert urls == ["https://loja-c-multi.com.br/fone", "https://www.kabum.com.br/produto/555"]
    assert "Fone" in c.get("/products").text


def test_migracao_de_produtos_antigos(tmp_path, monkeypatch):
    """Banco da versão anterior (sem itens): cada produto vira um item com uma oferta."""
    db_file = tmp_path / "antigo.db"
    con = sqlite3.connect(db_file)
    con.executescript("""
        CREATE TABLE stores (id INTEGER PRIMARY KEY, name VARCHAR(120), domain VARCHAR(255) UNIQUE,
            interval_minutes INTEGER, read_mode VARCHAR(16), created_at DATETIME);
        CREATE TABLE products (id INTEGER PRIMARY KEY, store_id INTEGER, url VARCHAR(2000) UNIQUE,
            name VARCHAR(300), image_url VARCHAR(2000), target_cents INTEGER, current_cents INTEGER,
            lowest_cents INTEGER, last_notified_cents INTEGER, available BOOLEAN, active BOOLEAN,
            last_checked_at DATETIME, last_error VARCHAR(300), created_at DATETIME);
        INSERT INTO stores VALUES (1, 'KaBuM!', 'kabum.com.br', 60, 'http', '2026-11-01');
        INSERT INTO products VALUES (7, 1, 'https://www.kabum.com.br/produto/1', 'Starlink', NULL, 60000,
            57999, 57999, NULL, 1, 1, NULL, NULL, '2026-11-01');
    """)
    con.commit()
    con.close()

    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from price_watch import db as dbmod
    engine = create_engine(f"sqlite:///{db_file}")
    monkeypatch.setattr(dbmod, "engine", engine)
    monkeypatch.setattr(dbmod, "SessionLocal", sessionmaker(engine, expire_on_commit=False))
    dbmod.init_db()
    with dbmod.SessionLocal() as s:
        item = s.get(Item, 7)
        assert item.name == "Starlink" and item.target_cents == 60000 and item.best_cents == 57999
        assert [o.id for o in item.offers] == [7]
