import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# Configuração isolada para os testes (não lê o .env real)
os.environ.setdefault("SECRET_KEY", "test-secret-key-0123456789")
os.environ.setdefault("DATA_DIR", tempfile.mkdtemp(prefix="pw-test-"))
