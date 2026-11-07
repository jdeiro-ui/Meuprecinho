import pytest
from fastapi.testclient import TestClient

from price_watch import main
from price_watch.db import SessionLocal, init_db
from price_watch.models import User
from price_watch.security import hash_password


@pytest.fixture
def clients():
    init_db()
    with SessionLocal() as db:
        for name, adm in (("adm_gm", True), ("hosp_gm", False)):
            u = db.query(User).filter_by(username=name).first() or User(username=name, password_hash=hash_password("senha-" + name))
            u.is_admin = adm
            db.add(u)
        db.commit()

    def login(name):
        main._fails.clear()
        c = TestClient(main.app)
        c.post("/login", data={"username": name, "password": "senha-" + name})
        return c

    return login("adm_gm"), login("hosp_gm")


def dock(c):
    return c.get("/ofertas").text.split('class="dock"')[1]


def test_modo_convidado_esconde_area_do_admin_e_volta(clients, monkeypatch):
    from price_watch.config import get_settings
    monkeypatch.setattr(get_settings(), "PRODUCTS_FOR_USERS", False)
    adm, _ = clients
    assert "Grupos" in dock(adm) and "Convites" in adm.get("/telegram").text
    adm.post("/modo-convidado/entrar")
    page = adm.get("/ofertas").text
    assert "Modo convidado" in page and "Grupos" not in dock(adm) and "Produtos" not in dock(adm)
    assert "Convites" not in adm.get("/telegram").text
    assert adm.get("/groups", follow_redirects=False).status_code == 303
    assert adm.get("/atualizacao", follow_redirects=False).status_code == 303
    adm.post("/modo-convidado/sair")
    assert "Grupos" in dock(adm) and "Convites" in adm.get("/telegram").text


def test_modo_convidado_nao_grava_nada_no_banco(clients):
    adm, _ = clients
    adm.post("/modo-convidado/entrar")
    adm.post("/conta/notificacoes", data={"channel": "app"})       # grava na conta durante o modo
    with SessionLocal() as db:
        assert db.query(User).filter_by(username="adm_gm").one().is_admin is True


def test_convidado_nao_consegue_entrar_no_modo(clients):
    _, hosp = clients
    hosp.post("/modo-convidado/entrar")
    assert "Modo convidado" not in hosp.get("/ofertas").text
