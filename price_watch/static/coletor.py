#!/usr/bin/env python3
"""Coletor de casa do MeuPrecinho.

Roda no Termux (Android) ou em qualquer computador com Python 3 e curl. Pergunta ao
servidor quais produtos consultar, baixa as páginas pela internet de casa (IP
residencial, aceito por lojas que bloqueiam servidores na nuvem) e devolve o HTML.
Não guarda nada além do endereço do servidor e do token.

Primeira vez:  python coletor.py https://SEU-SERVIDOR TOKEN
Depois:        python coletor.py
"""

import gzip
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

VERSION = 1
CONF = os.path.expanduser("~/.meuprecinho-coletor.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
DOMAIN_GAP = 5      # segundos entre acessos à mesma loja
MAX_PAGE = 8 << 20  # 8 MB


def log(msg: str) -> None:
    print(time.strftime("%H:%M:%S"), msg, flush=True)


def load_conf() -> dict:
    if len(sys.argv) >= 3:
        conf = {"server": sys.argv[1].rstrip("/"), "token": sys.argv[2]}
        with open(CONF, "w") as f:
            json.dump(conf, f)
        os.chmod(CONF, 0o600)
        return conf
    try:
        with open(CONF) as f:
            return json.load(f)
    except (OSError, ValueError):
        sys.exit("Rode a primeira vez com: python coletor.py https://SEU-SERVIDOR TOKEN")


def api(conf: dict, path: str, params: dict | None = None, body: bytes | None = None):
    url = conf["server"] + path + ("?" + urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, data=body, method="POST" if body is not None else "GET")
    req.add_header("Authorization", "Bearer " + conf["token"])
    req.add_header("X-Coletor-Versao", str(VERSION))
    if body is not None:
        req.add_header("Content-Type", "application/octet-stream")
        req.add_header("Content-Encoding", "gzip")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read() or b"{}")


def fetch(url: str) -> tuple[int, bytes, str]:
    """Baixa com curl (mesmo cliente que passou nos testes de bloqueio)."""
    cmd = ["curl", "-sL", "--compressed", "--max-time", "30", "-A", UA,
           "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
           "-H", "Accept-Language: pt-BR,pt;q=0.9,en;q=0.6",
           "-w", "\n%{http_code}", url]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=45).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        return 0, b"", f"falha no curl ({type(e).__name__})"
    body, _, code = out.rpartition(b"\n")
    status = int(code) if code.strip().isdigit() else 0
    if status == 0:
        return 0, b"", "sem resposta da loja"
    return status, body[:MAX_PAGE], ""


def main() -> None:
    conf = load_conf()
    try:
        subprocess.run(["termux-wake-lock"], timeout=10)  # impede o Android de pausar o Termux
    except (OSError, subprocess.TimeoutExpired):
        pass
    log(f"Coletor ligado (servidor {urllib.parse.urlsplit(conf['server']).hostname}).")
    log("Deixe esta tela aberta. Para parar: Ctrl + C.")
    last_hit: dict[str, float] = {}
    offline = False
    while True:
        try:
            jobs = api(conf, "/api/coletor/jobs").get("jobs", [])
            if offline:
                log("Contato com o servidor restabelecido.")
                offline = False
        except urllib.error.HTTPError as e:
            if e.code == 401:
                log("Token recusado. Copie de novo o comando na tela Conta do app.")
                time.sleep(300)
            else:
                log(f"Servidor respondeu HTTP {e.code}; nova tentativa em 1 min.")
                time.sleep(60)
            continue
        except Exception as e:  # noqa: BLE001 — sem rede, servidor fora etc.
            if not offline:
                log(f"Sem contato com o servidor ({type(e).__name__}); tentando a cada minuto.")
                offline = True
            time.sleep(60)
            continue

        for job in jobs:
            host = urllib.parse.urlsplit(job["url"]).hostname or ""
            wait = DOMAIN_GAP - (time.time() - last_hit.get(host, 0))
            if wait > 0:
                time.sleep(wait)
            status, body, error = fetch(job["url"])
            last_hit[host] = time.time()
            params = {"id": job["id"], "status": status}
            if error:
                params["error"] = error
            try:
                api(conf, "/api/coletor/result", params, gzip.compress(body))
                log(f"Produto {job['id']}: {host} -> " + (error or f"HTTP {status}"))
            except Exception as e:  # noqa: BLE001
                log(f"Produto {job['id']}: não consegui enviar ao servidor ({type(e).__name__})")
        time.sleep(3 if jobs else 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log("Coletor parado.")
