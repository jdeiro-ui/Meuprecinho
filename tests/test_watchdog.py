import asyncio
from datetime import timedelta

from price_watch import monitor, notifier
from price_watch.db import SessionLocal, init_db
from price_watch.models import Item, Product, Setting, Store, utcnow


def _run(monkeypatch, sent):
    async def fake_deliver(user_id, html, **kw):
        sent.append(html)
        return True

    monkeypatch.setattr(notifier, "deliver", fake_deliver)
    monitor.collector.update(last_seen=None, warned=False)
    asyncio.run(monitor.collector_watchdog(utcnow() - timedelta(hours=2)))


def test_vigia_do_coletor_sem_alarme_falso(monkeypatch):
    init_db()
    sent = []
    with SessionLocal() as db:
        db.query(Setting).filter_by(key="collector_installed").delete()
        db.query(Product).delete()
        amazon = db.query(Store).filter_by(domain="amazon.com.br").first() or \
            Store(name="Amazon", domain="amazon.com.br", fetch_via="home")
        amazon.fetch_via = "home"
        item = Item(name="Kindle")
        db.add_all([item, Product(item=item, store=amazon, url="https://www.zoom.com.br/lead?oid=9", zoom_oid="9")])
        db.commit()
        iid, store_id = item.id, amazon.id

    _run(monkeypatch, sent)
    assert sent == []          # só oferta via Zoom e coletor nunca instalado: nada de aviso

    monitor._remember_collector_installed()
    _run(monkeypatch, sent)
    assert sent == []          # instalado, mas nenhuma oferta depende dele

    with SessionLocal() as db:
        db.add(Product(item_id=iid, store_id=store_id, url="https://www.amazon.com.br/dp/B0KINDLE01"))
        db.commit()
    _run(monkeypatch, sent)
    assert len(sent) == 1 and "Coletor de casa sem contato" in sent[0]   # agora sim
