from fastapi.testclient import TestClient

from price_watch import main
from price_watch.db import SessionLocal, init_db
from price_watch.models import User
from price_watch.security import hash_password


def test_bloqueia_apos_muitas_senhas_erradas(monkeypatch):
    init_db()
    with SessionLocal() as db:
        if not db.query(User).filter_by(username="admin").first():
            db.add(User(username="admin", password_hash=hash_password("senha-certa")))
            db.commit()

    async def no_sleep(_):
        return None

    monkeypatch.setattr(main.asyncio, "sleep", no_sleep)
    main._fails.clear()
    c = TestClient(main.app)
    for _ in range(main.MAX_FAILS):
        assert "inválidos" in c.post("/login", data={"username": "admin", "password": "x"}).text
    r = c.post("/login", data={"username": "admin", "password": "senha-certa"})
    assert "Muitas tentativas" in r.text
    main._fails.clear()
    r = c.post("/login", data={"username": "admin", "password": "senha-certa"})
    assert r.url.path == "/alerts"
