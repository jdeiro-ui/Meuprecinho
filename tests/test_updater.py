import subprocess

import pytest
from fastapi.testclient import TestClient

from price_watch import main, updater
from price_watch.db import SessionLocal, init_db
from price_watch.models import User
from price_watch.security import hash_password


def sh(cwd, *cmd):
    subprocess.run(cmd, cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def repos(tmp_path, monkeypatch):
    origin, work, other = tmp_path / "o.git", tmp_path / "w", tmp_path / "x"
    sh(tmp_path, "git", "init", "-q", "--bare", "-b", "main", str(origin))
    cfg = ["-c", "user.name=t", "-c", "user.email=t@t"]
    sh(tmp_path, "git", "clone", "-q", str(origin), str(work))
    (work / "a.txt").write_text("1")
    sh(work, "git", "add", "."); sh(work, "git", *cfg, "commit", "-qm", "primeira"); sh(work, "git", "push", "-q", "origin", "HEAD:main")
    sh(work, "git", "branch", "-q", "--set-upstream-to=origin/main")
    sh(tmp_path, "git", "clone", "-q", str(origin), str(other))
    (other / "a.txt").write_text("2")
    sh(other, "git", *cfg, "commit", "-qam", "segunda"); sh(other, "git", "push", "-q", "origin", "HEAD:main")
    monkeypatch.setattr(updater, "ROOT", work)
    monkeypatch.setattr(updater, "restart_mode", lambda: ("price-watch", True))
    return work


@pytest.mark.asyncio
async def test_verifica_e_atualiza(repos, monkeypatch):
    monkeypatch.setattr(updater, "_import_ok", lambda: True)
    monkeypatch.setattr(updater.asyncio.get_running_loop().__class__, "call_later", lambda *a, **k: None)
    info = await updater.check()
    assert info["behind"] == 1 and info["incoming"] == ["segunda"]
    res = await updater.apply()
    assert res == {"ok": True, "changed": True, "restarting": True}
    assert (repos / "a.txt").read_text() == "2"
    assert (await updater.apply())["changed"] is False


@pytest.mark.asyncio
async def test_volta_atras_se_codigo_novo_nao_carrega(repos, monkeypatch):
    monkeypatch.setattr(updater, "_import_ok", lambda: False)
    res = await updater.apply()
    assert res["ok"] is False and "anterior" in res["error"]
    assert (repos / "a.txt").read_text() == "1"


@pytest.mark.asyncio
async def test_sem_reinicio_automatico_mostra_comando(repos, monkeypatch):
    monkeypatch.setattr(updater, "_import_ok", lambda: True)
    monkeypatch.setattr(updater, "restart_mode", lambda: ("price-watch", False))
    info = await updater.check()
    assert "Restart=always" in info["enable_cmd"]
    res = await updater.apply()
    assert res["restarting"] is False and "price-watch.service.d" not in res["enable_cmd"] and "Restart=always" in res["enable_cmd"]


def test_so_administrador():
    init_db()
    with SessionLocal() as db:
        if not db.query(User).filter_by(username="comum_upd").first():
            db.add(User(username="comum_upd", password_hash=hash_password("senha-comum1"), is_admin=False))
            db.commit()
    main._fails.clear()
    c = TestClient(main.app)
    c.post("/login", data={"username": "comum_upd", "password": "senha-comum1"})
    assert c.get("/atualizacao", follow_redirects=False).status_code != 200
    assert c.post("/atualizacao/aplicar", follow_redirects=False).status_code != 200
