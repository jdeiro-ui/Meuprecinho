"""Atualização do próprio app pela tela Conta (só administrador).

Busca a versão nova no git, instala dependências se mudaram, confere que o código
importa e, só então, reinicia. Se algo falha, volta para a versão anterior."""

import asyncio
import os
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
_busy = asyncio.Lock()

# Comando único (rodado uma vez no SSH) para o systemd religar o app depois de encerrado
ENABLE_RESTART = ("sudo mkdir -p /etc/systemd/system/{u}.d && printf '[Service]\\nRestart=always\\n"
                  "RestartSec=2\\n' | sudo tee /etc/systemd/system/{u}.d/restart.conf >/dev/null "
                  "&& sudo systemctl daemon-reload && sudo systemctl restart {u}")


def _run(*cmd: str, timeout: int = 120) -> str:
    p = subprocess.run(cmd, cwd=ROOT, env=_ENV, capture_output=True, text=True, timeout=timeout)
    if p.returncode:
        raise RuntimeError((p.stderr or p.stdout).strip().splitlines()[-1][:200] if (p.stderr or p.stdout).strip()
                           else f"{cmd[0]} falhou")
    return p.stdout.strip()


def _git(*args: str, timeout: int = 60) -> str:
    return _run("git", *args, timeout=timeout)


def _unit() -> str | None:
    """Nome do serviço systemd em que o app roda (None se foi iniciado à mão)."""
    if "INVOCATION_ID" not in os.environ:
        return None
    try:
        m = re.search(r"([\w@.-]+)\.service", Path("/proc/self/cgroup").read_text())
    except OSError:
        return None
    return m.group(1) if m else None


def restart_mode() -> tuple[str | None, bool]:
    """(serviço, reinicia sozinho ao encerrar?)"""
    unit = _unit()
    if not unit:
        return None, False
    try:
        policy = subprocess.run(["systemctl", "show", unit, "-p", "Restart", "--value"],
                                capture_output=True, text=True, timeout=10).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        policy = ""
    return unit, policy in ("always", "on-failure")


def _check() -> dict:
    if not (ROOT / ".git").exists():
        return {"error": "esta instalação não é um clone do git"}
    cur = _git("log", "-1", "--format=%h|%cd|%s", "--date=format:%d/%m %H:%M").split("|", 2)
    info = {"current": {"sha": cur[0], "date": cur[1], "subject": cur[2]}, "behind": 0, "incoming": []}
    try:
        _git("fetch", "-q", timeout=90)
        info["behind"] = int(_git("rev-list", "--count", "HEAD..@{u}"))
        if info["behind"]:
            info["incoming"] = _git("log", "HEAD..@{u}", "--format=%s", "-n", "8").splitlines()
    except (RuntimeError, ValueError, subprocess.SubprocessError) as e:
        info["error"] = f"não consegui consultar o repositório ({e})"
    unit, auto = restart_mode()
    info |= {"unit": unit, "auto_restart": auto}
    if unit and not auto:
        info["enable_cmd"] = ENABLE_RESTART.format(u=unit)
    return info


def _import_ok() -> bool:
    p = subprocess.run([sys.executable, "-c", "import price_watch.main"], cwd=ROOT, env=_ENV,
                       capture_output=True, timeout=120)
    return p.returncode == 0


def _apply() -> dict:
    prev = _git("rev-parse", "HEAD")
    try:
        _git("pull", "--ff-only", "-q", timeout=120)
        new = _git("rev-parse", "HEAD")
        if new == prev:
            return {"ok": True, "changed": False}
        if "requirements.txt" in _git("diff", "--name-only", prev, new).splitlines():
            _run(sys.executable, "-m", "pip", "install", "-q", "-r", "requirements.txt", timeout=300)
        if not _import_ok():
            raise RuntimeError("o código novo não carregou")
    except (RuntimeError, subprocess.SubprocessError) as e:
        try:
            _git("reset", "--hard", prev)
        except RuntimeError:
            pass
        return {"ok": False, "error": f"{e}. Mantive a versão anterior."}
    return {"ok": True, "changed": True}


async def check() -> dict:
    return await asyncio.to_thread(_check)


async def apply() -> dict:
    """Atualiza e agenda o reinício. Só uma atualização por vez."""
    if _busy.locked():
        return {"ok": False, "error": "já existe uma atualização em andamento"}
    async with _busy:
        res = await asyncio.to_thread(_apply)
    if res.get("ok") and res.get("changed"):
        unit, auto = restart_mode()
        res["restarting"] = auto
        if auto:  # código de saída ≠ 0: o systemd (Restart=always|on-failure) religa em seguida
            asyncio.get_running_loop().call_later(1.5, os._exit, 1)
        elif unit:
            res["enable_cmd"] = ENABLE_RESTART.format(u=unit)
    return res
