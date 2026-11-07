import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Configuração isolada para os testes (não lê o .env real)
os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789")
os.environ.setdefault("PRODUCTS_FOR_USERS", "true")  # os testes antigos usam Produtos como convidado
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="pw-test-"))

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _sem_busca_real(monkeypatch):
    """A busca automática em outras lojas não vai à internet nos testes."""
    from price_watch import discovery

    monkeypatch.setattr(discovery, "SEARCHERS", {})
    monkeypatch.setattr(discovery, "ZOOM_ENABLED", False)


@pytest.fixture(autouse=True)
def _sem_rede_nos_encurtadores(monkeypatch):
    from price_watch import offers

    monkeypatch.setattr(offers, "resolve", lambda url: None)
