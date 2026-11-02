import asyncio
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import mercadolivre, monitor, notifier, scheduler
from .config import get_settings
from .db import init_db
from .models import Alert, Rule, TgGroup, User
from .routes_collector import collector_token
from .routes_collector import router as collector_router
from .routes_prices import router as prices_router
from .security import verify_password
from .telegram import PairingError, manager
from .web import DB, Auth, LoginRequired, back, flash, render

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)  # a URL do bot contém o token
logging.getLogger("apscheduler").setLevel(logging.WARNING)

BASE = Path(__file__).parent
settings = get_settings()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    await manager.start()
    scheduler.start()
    yield
    scheduler.stop()
    await manager.stop()
    await notifier.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.SECRET_KEY,
    session_cookie="pw_session",
    max_age=settings.SESSION_DAYS * 86400,
    same_site="lax",  # bloqueia POST vindo de outros sites (CSRF básico)
    https_only=settings.COOKIE_SECURE,
)
app.mount("/static", StaticFiles(directory=BASE / "static"), name="static")
app.include_router(prices_router)
app.include_router(collector_router)


# ---------- autenticação ----------

@app.exception_handler(LoginRequired)
async def _to_login(request: Request, _: LoginRequired):
    if request.headers.get("HX-Request"):
        return Response(status_code=401, headers={"HX-Redirect": "/login"})
    if request.method == "GET":  # volta para a página pedida depois do login (ex.: compartilhar)
        q = request.url.query
        request.session["next"] = request.url.path + (f"?{q}" if q else "")
    return RedirectResponse("/login", status_code=303)


@app.get("/login")
def login_form(request: Request):
    return render(request, "login.html")


# Bloqueio por IP após muitas senhas erradas (em memória; zera ao reiniciar)
MAX_FAILS, FAIL_WINDOW = 5, 15 * 60
_fails: dict[str, list[float]] = {}


def _recent_fails(ip: str) -> list[float]:
    now = time.monotonic()
    recent = [t for t in _fails.get(ip, []) if now - t < FAIL_WINDOW]
    if recent:
        _fails[ip] = recent
    else:
        _fails.pop(ip, None)
    return recent


@app.post("/login")
async def login(request: Request, db: DB, username: str = Form(), password: str = Form()):
    ip = request.client.host if request.client else "?"
    if len(_recent_fails(ip)) >= MAX_FAILS:
        return render(request, "login.html", error="Muitas tentativas. Tente de novo em 15 minutos.")
    user = db.scalar(select(User).where(User.username == username.strip()))
    if user and await asyncio.to_thread(verify_password, password, user.password_hash):
        _fails.pop(ip, None)
        nxt = request.session.get("next") or "/"
        request.session.clear()
        request.session["uid"] = user.id
        return back(nxt if nxt.startswith("/") and not nxt.startswith("//") else "/")
    _fails.setdefault(ip, []).append(time.monotonic())
    await asyncio.sleep(1.5)  # freia tentativas de força bruta
    return render(request, "login.html", error="Usuário ou senha inválidos")


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return back("/login")


# ---------- PWA ----------

@app.get("/manifest.webmanifest")
def manifest():
    # Sem cache, para nome e ícone novos chegarem logo ao Android
    return FileResponse(BASE / "static/manifest.webmanifest", media_type="application/manifest+json",
                        headers={"Cache-Control": "no-cache"})


@app.get("/sw.js")
def service_worker():
    # Servido na raiz para que o escopo do service worker seja o app inteiro
    return FileResponse(BASE / "static/sw.js", media_type="text/javascript",
                        headers={"Cache-Control": "no-cache"})


@app.get("/healthz")
def healthz():
    return {"ok": True, "telegram": manager.connected}


@app.get("/", dependencies=[Auth])
def home():
    return back("/alerts")


# ---------- avisos ----------

@app.get("/alerts", dependencies=[Auth])
def alerts(request: Request, db: DB):
    items = db.scalars(select(Alert).order_by(Alert.created_at.desc()).limit(100)).all()
    return render(request, "alerts.html", alerts=items, bot_ok=settings.bot_configured)


@app.post("/alerts/test", dependencies=[Auth])
async def alerts_test(request: Request):
    ok = await notifier.send("✅ MeuPrecinho: teste de aviso")
    if ok:
        flash(request, "Mensagem de teste enviada")
    else:
        hint = {"Unauthorized": "BOT_TOKEN inválido",
                "Bad Request: chat not found": "CHAT_ID errado ou você não tocou em Iniciar no bot",
                "Forbidden: bot was blocked by the user": "o bot está bloqueado; desbloqueie e toque em Iniciar"}
        why = notifier.last_error or "veja BOT_TOKEN/CHAT_ID"
        flash(request, f"Falha ao enviar: {hint.get(why, why)}", "err")
    return back("/alerts")


# ---------- grupos ----------

@app.get("/groups", dependencies=[Auth])
def groups(request: Request, db: DB):
    items = db.scalars(select(TgGroup).order_by(TgGroup.monitored.desc(), TgGroup.title)).all()
    return render(request, "groups.html", groups=items)


@app.post("/groups/refresh", dependencies=[Auth])
async def groups_refresh(request: Request):
    try:
        n = await manager.refresh_groups()
        flash(request, f"{n} grupos/canais encontrados")
    except PairingError as e:
        flash(request, str(e), "err")
    return back("/groups")


@app.post("/groups/{group_id}/toggle", dependencies=[Auth])
async def group_toggle(request: Request, db: DB, group_id: int):
    g = db.get(TgGroup, group_id)
    if g is None:
        return Response(status_code=404)
    g.monitored = not g.monitored
    db.commit()
    await asyncio.to_thread(manager.reload)
    return render(request, "_group_row.html", g=g)


# ---------- regras ----------

def _rules_page(request: Request, db: Session, edit: Rule | None = None):
    rules_ = db.scalars(select(Rule).order_by(Rule.active.desc(), Rule.keyword)).all()
    groups_ = db.scalars(select(TgGroup).where(TgGroup.monitored).order_by(TgGroup.title)).all()
    return render(request, "rules.html", rules=rules_, groups=groups_, edit=edit)


@app.get("/rules", dependencies=[Auth])
def rules(request: Request, db: DB):
    return _rules_page(request, db)


@app.get("/rules/{rule_id}", dependencies=[Auth])
def rule_edit(request: Request, db: DB, rule_id: int):
    return _rules_page(request, db, db.get(Rule, rule_id))


@app.post("/rules", dependencies=[Auth])
@app.post("/rules/{rule_id}", dependencies=[Auth])
async def rule_save(request: Request, db: DB, keyword: str = Form(), exclusions: str = Form(""),
                    group_ids: list[int] = Form(default=[]), active: bool = Form(False),
                    rule_id: int | None = None):
    keyword = " ".join(keyword.split())
    if not keyword.rstrip("*"):
        flash(request, "Informe a palavra-chave", "err")
        return back("/rules")
    rule = db.get(Rule, rule_id) if rule_id else Rule()
    if rule is None:
        return back("/rules")
    if rule.item_id and keyword != rule.keyword:
        rule.auto = False  # palavra editada à mão: o app não sobrescreve mais
    rule.keyword = keyword
    rule.exclusions = ", ".join(t.strip() for t in exclusions.split(",") if t.strip())
    rule.active = active
    rule.groups = list(db.scalars(select(TgGroup).where(TgGroup.id.in_(group_ids)))) if group_ids else []
    db.add(rule)
    db.commit()
    await asyncio.to_thread(manager.reload)
    flash(request, "Regra salva")
    return back("/rules")


@app.post("/rules/{rule_id}/toggle", dependencies=[Auth])
async def rule_toggle(request: Request, db: DB, rule_id: int):
    r = db.get(Rule, rule_id)
    if r is None:
        return Response(status_code=404)
    r.active = not r.active
    db.commit()
    await asyncio.to_thread(manager.reload)
    return render(request, "_rule_row.html", r=r)


@app.post("/rules/{rule_id}/delete", dependencies=[Auth])
async def rule_delete(request: Request, db: DB, rule_id: int):
    if r := db.get(Rule, rule_id):
        db.delete(r)
        db.commit()
        await asyncio.to_thread(manager.reload)
    if request.headers.get("HX-Request"):
        return Response("")
    return back("/rules")


# ---------- pareamento do Telegram ----------

def _base(request: Request) -> str:
    base = str(request.base_url).rstrip("/")
    if settings.COOKIE_SECURE and base.startswith("http://"):  # atrás de proxy HTTPS (Tailscale, Caddy)
        base = "https://" + base.removeprefix("http://")
    return base


@app.get("/telegram", dependencies=[Auth])
def telegram_page(request: Request):
    base = _base(request)
    token = collector_token()
    install = (f"pkg install -y python curl && curl -fsSL {base}/coletor.py -o ~/coletor.py "
               f"&& python ~/coletor.py {base} {token}")
    return render(request, "telegram.html", configured=settings.telegram_configured,
                  install=install, silent=monitor.collector_silent_for(),
                  ml_configured=mercadolivre.configured(), ml=mercadolivre.status(),
                  ml_redirect=f"{base}/ml/callback")


# ---------- Mercado Livre (OAuth) ----------

@app.get("/ml/connect", dependencies=[Auth])
def ml_connect(request: Request):
    if not mercadolivre.configured():
        flash(request, "Preencha ML_CLIENT_ID e ML_CLIENT_SECRET no .env e reinicie o app", "err")
        return back("/telegram")
    verifier, challenge = mercadolivre.new_pkce()
    state = secrets.token_urlsafe(16)
    request.session["ml_oauth"] = {"state": state, "verifier": verifier}
    return RedirectResponse(mercadolivre.authorize_url(f"{_base(request)}/ml/callback", state, challenge))


@app.get("/ml/callback", dependencies=[Auth])
async def ml_callback(request: Request, code: str = "", state: str = "", error: str = ""):
    pending = request.session.pop("ml_oauth", None)
    if error or not code or not pending or not secrets.compare_digest(state, pending["state"]):
        flash(request, "Conexão com o Mercado Livre cancelada ou inválida. Tente de novo.", "err")
        return back("/telegram#ml")
    try:
        await mercadolivre.exchange_code(code, f"{_base(request)}/ml/callback", pending["verifier"])
        flash(request, "Mercado Livre conectado")
    except Exception as e:  # noqa: BLE001
        flash(request, f"Falha ao conectar: {e}", "err")
    return back("/telegram#ml")


@app.post("/ml/disconnect", dependencies=[Auth])
def ml_disconnect(request: Request):
    mercadolivre.disconnect()
    flash(request, "Mercado Livre desconectado")
    return back("/telegram#ml")


@app.post("/coletor/token", dependencies=[Auth])
def collector_new_token(request: Request):
    collector_token(regenerate=True)
    flash(request, "Token novo gerado. Rode o comando de instalação de novo no celular.")
    return back("/telegram")


async def _pairing_step(request: Request, coro) -> RedirectResponse:
    try:
        await coro
    except PairingError as e:
        flash(request, str(e), "err")
    except Exception as e:  # noqa: BLE001 — erros do Telegram viram aviso na tela
        flash(request, f"Erro do Telegram: {type(e).__name__}", "err")
    return back("/telegram")


@app.post("/telegram/phone", dependencies=[Auth])
async def tg_phone(request: Request, phone: str = Form()):
    phone = "+" + "".join(c for c in phone if c.isdigit())
    return await _pairing_step(request, manager.send_code(phone))


@app.post("/telegram/code", dependencies=[Auth])
async def tg_code(request: Request, code: str = Form()):
    return await _pairing_step(request, manager.sign_in_code(code))


@app.post("/telegram/password", dependencies=[Auth])
async def tg_password(request: Request, password: str = Form()):
    return await _pairing_step(request, manager.sign_in_password(password))


@app.post("/telegram/logout", dependencies=[Auth])
async def tg_logout(request: Request):
    return await _pairing_step(request, manager.logout())
