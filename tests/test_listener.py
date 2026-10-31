import asyncio
from types import SimpleNamespace

from sqlalchemy import select

from price_watch import notifier
from price_watch.db import SessionLocal, init_db
from price_watch.models import Alert
from price_watch.rules import RuleSpec
from price_watch.telegram import GroupInfo, TelegramManager


def test_listener_filtra_grupo_aplica_regra_e_nao_repete(monkeypatch):
    init_db()
    sent = []

    async def fake_send(text):
        sent.append(text)
        return True

    monkeypatch.setattr(notifier, "send", fake_send)
    m = TelegramManager()
    m.groups = {-100500: GroupInfo("Promos", "promos")}
    m.rules = [RuleSpec(id=1, keyword="air fryer")]

    def ev(chat_id, msg_id, text):
        return SimpleNamespace(chat_id=chat_id, id=msg_id, raw_text=text)

    async def run():
        await m._on_message(ev(-100999, 1, "air fryer"))  # grupo não marcado
        await m._on_message(ev(-100500, 2, "nada a ver"))  # sem palavra
        await m._on_message(ev(-100500, 3, "AIR FRYER barata"))
        await m._on_message(ev(-100500, 3, "AIR FRYER barata"))  # repetida

    asyncio.run(run())
    assert len(sent) == 1
    assert "Promos" in sent[0] and "https://t.me/promos/3" in sent[0]
    with SessionLocal() as db:
        alerts = db.scalars(select(Alert).where(Alert.link == "https://t.me/promos/3")).all()
    assert len(alerts) == 1 and alerts[0].delivered
