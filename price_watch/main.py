import asyncio
import logging
import re
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

from . import bot, item_rules, mercadolivre, monitor, notifier, scheduler
from .config import get_settings
from .db import init_db
from .models import Alert, Invite, Rule, TgGroup, User, utcnow
from .routes_collector import collector_token
from .routes_collector import router as collector_router
from .routes_prices import router as prices_router
from .pricing import parse_brl
from .security import hash_password, verify_password
from .telegram import PairingError, manager
from .web import DB, AdminOnly, AdminRequired, Auth, LoginRequired, Me, back, flash, render

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)  # a URL do bot contém o token
logging.getLogger("apscheduler").setLevel(logging.WARNING)

BASE = Path(__file__).parent
settings = get_settings()


def _admin_chat_from_env() -> None:
    """Antes das contas, os avisos iam para CHAT_ID do .env: ele vira a conversa do admin."""
    if not settings.CHAT_ID:
        return
    from .db import SessionLocal

    with SessionLocal() as db:
        admin = db.scalar(select(User).where(User.is_admin).order_by(User.id))
        if admin and not admin.telegram_chat_id:
            admin.telegram_chat_id = settings.CHAT_ID
            db.commit()


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    _admin_chat_from_env()
    item_rules.resync_all()  # regras automáticas dos produtos sempre com o gerador atual
    await manager.start()
    bot_task = asyncio.create_task(bot.run()) if settings.BOT_TOKEN else None
    scheduler.start()
    yield
    if bot_task:
        bot_task.cancel()
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

@app.exception_handler(AdminRequired)
async def _not_admin(request: Request, _: AdminRequired):
    if request.headers.get("HX-Request"):
        return Response(status_code=403)
    flash(request, "Essa área é só do administrador", "err")
    return RedirectResponse("/products", status_code=303)


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
    return back("/products")


# ---------- avisos ----------

@app.get("/alerts")
def alerts(request: Request, db: DB, me: Me):
    items = db.scalars(select(Alert).where(Alert.user_id == me.id)
                       .order_by(Alert.created_at.desc()).limit(100)).all()
    return render(request, "alerts.html", alerts=items, bot_ok=bool(me.telegram_chat_id))


@app.post("/alerts/test")
async def alerts_test(request: Request, me: Me):
    if not me.telegram_chat_id:
        flash(request, "Conecte o Telegram na aba Conta para receber os avisos", "err")
        return back("/telegram")
    ok = await notifier.send("✅ MeuPrecinho: teste de aviso", chat_id=me.telegram_chat_id)
    if ok:
        flash(request, "Mensagem de teste enviada")
    else:
        hint = {"Unauthorized": "BOT_TOKEN inválido",
                "Bad Request: chat not found": "conversa com o bot não encontrada; conecte de novo na aba Conta",
                "Forbidden: bot was blocked by the user": "o bot está bloqueado; desbloqueie e toque em Iniciar"}
        why = notifier.last_error or "veja BOT_TOKEN/CHAT_ID"
        flash(request, f"Falha ao enviar: {hint.get(why, why)}", "err")
    return back("/alerts")


# ---------- grupos ----------

@app.get("/groups", dependencies=[AdminOnly])
def groups(request: Request, db: DB):
    items = db.scalars(select(TgGroup).order_by(TgGroup.monitored.desc(), TgGroup.title)).all()
    return render(request, "groups.html", groups=items)


@app.post("/groups/refresh", dependencies=[AdminOnly])
async def groups_refresh(request: Request):
    try:
        n = await manager.refresh_groups()
        flash(request, f"{n} grupos/canais encontrados")
    except PairingError as e:
        flash(request, str(e), "err")
    return back("/groups")


@app.post("/groups/{group_id}/toggle", dependencies=[AdminOnly])
async def group_toggle(request: Request, db: DB, group_id: int):
    g = db.get(TgGroup, group_id)
    if g is None:
        return Response(status_code=404)
    g.monitored = not g.monitored
    db.commit()
    await asyncio.to_thread(manager.reload)
    return render(request, "_group_row.html", g=g)


# ---------- regras ----------

def _own_rule(db: Session, me: User, rule_id: int) -> Rule | None:
    rule = db.get(Rule, rule_id)
    return rule if rule is not None and rule.user_id == me.id else None


def _rules_page(request: Request, db: Session, me: User, edit: Rule | None = None):
    rules_ = db.scalars(select(Rule).where(Rule.user_id == me.id)
                        .order_by(Rule.active.desc(), Rule.keyword)).all()
    groups_ = db.scalars(select(TgGroup).where(TgGroup.monitored).order_by(TgGroup.title)).all()
    return render(request, "rules.html", rules=rules_, groups=groups_, edit=edit)


@app.get("/rules")
def rules(request: Request, db: DB, me: Me):
    return _rules_page(request, db, me)


@app.get("/rules/{rule_id}")
def rule_edit(request: Request, db: DB, me: Me, rule_id: int):
    return _rules_page(request, db, me, _own_rule(db, me, rule_id))


@app.post("/rules")
@app.post("/rules/{rule_id}")
async def rule_save(request: Request, db: DB, me: Me, keyword: str = Form(), exclusions: str = Form(""),
                    group_ids: list[int] = Form(default=[]), active: bool = Form(False),
                    max_price: str = Form(""), rule_id: int | None = None):
    keyword = " ".join(keyword.split())
    if not keyword.rstrip("*"):
        flash(request, "Informe a palavra-chave", "err")
        return back("/rules")
    rule = _own_rule(db, me, rule_id) if rule_id else Rule(user_id=me.id)
    if rule is None:
        return back("/rules")
    if rule.item_id and keyword != rule.keyword:
        rule.auto = False  # palavra editada à mão: o app não sobrescreve mais
    rule.keyword = keyword
    rule.exclusions = ", ".join(t.strip() for t in exclusions.split(",") if t.strip())
    rule.active = active
    rule.max_price_cents = parse_brl(max_price)  # vazio = qualquer preço
    rule.groups = list(db.scalars(select(TgGroup).where(TgGroup.id.in_(group_ids)))) if group_ids else []
    db.add(rule)
    db.commit()
    await asyncio.to_thread(manager.reload)
    flash(request, "Regra salva")
    return back("/rules")


@app.post("/rules/{rule_id}/toggle")
async def rule_toggle(request: Request, db: DB, me: Me, rule_id: int):
    r = _own_rule(db, me, rule_id)
    if r is None:
        return Response(status_code=404)
    r.active = not r.active
    db.commit()
    await asyncio.to_thread(manager.reload)
    return render(request, "_rule_row.html", r=r)


@app.post("/rules/{rule_id}/delete")
async def rule_delete(request: Request, db: DB, me: Me, rule_id: int):
    if r := _own_rule(db, me, rule_id):
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


@app.get("/telegram")
async def telegram_page(request: Request, db: DB, me: Me):
    ctx = {"bot_link": await bot.connect_link(me.id) if not me.telegram_chat_id else None}
    if me.is_admin:
        base = _base(request)
        token = collector_token()
        ctx |= {
            "configured": settings.telegram_configured,
            "install": (f"pkg install -y python curl && curl -fsSL {base}/coletor.py -o ~/coletor.py "
                        f"&& python ~/coletor.py {base} {token}"),
            "silent": monitor.collector_silent_for(),
            "ml_configured": mercadolivre.configured(), "ml": mercadolivre.status(),
            "ml_redirect": f"{base}/ml/callback",
            "invites": db.scalars(select(Invite).order_by(Invite.created_at.desc()).limit(30)).all(),
            "users": {u.id: u.username for u in db.scalars(select(User))},
            "invite_base": f"{base}/convite/",
        }
    return render(request, "telegram.html", **ctx)


@app.post("/conta/telegram/desconectar")
async def account_tg_disconnect(request: Request, db: DB, me: Me):
    me.telegram_chat_id = None
    db.commit()
    await asyncio.to_thread(manager.reload)
    flash(request, "Telegram desconectado: os avisos param de chegar")
    return back("/telegram")


@app.post("/conta/senha")
async def account_password(request: Request, db: DB, me: Me, current: str = Form(), new: str = Form()):
    if not await asyncio.to_thread(verify_password, current, me.password_hash):
        flash(request, "Senha atual incorreta", "err")
    elif len(new) < 8:
        flash(request, "A nova senha precisa ter pelo menos 8 caracteres", "err")
    else:
        me.password_hash = await asyncio.to_thread(hash_password, new)
        db.commit()
        flash(request, "Senha alterada")
    return back("/telegram")


# ---------- convites (lançamento fechado) ----------

@app.post("/convites", dependencies=[AdminOnly])
def invite_create(request: Request, db: DB, note: str = Form("")):
    db.add(Invite(code=secrets.token_urlsafe(9), note=note.strip()[:120]))
    db.commit()
    flash(request, "Convite criado: copie o link e mande para a pessoa")
    return back("/telegram#convites")


@app.post("/convites/{invite_id}/delete", dependencies=[AdminOnly])
def invite_delete(request: Request, db: DB, invite_id: int):
    inv = db.get(Invite, invite_id)
    if inv and inv.used_by is None:
        db.delete(inv)
        db.commit()
    return back("/telegram#convites")


_USERNAME = re.compile(r"^[a-z0-9._-]{3,32}$")


def _open_invite(db: Session, code: str) -> Invite | None:
    inv = db.scalar(select(Invite).where(Invite.code == code))
    return inv if inv is not None and inv.used_by is None else None


@app.get("/convite/{code}")
def signup_form(request: Request, db: DB, code: str):
    return render(request, "signup.html", invite=_open_invite(db, code), code=code)


@app.post("/convite/{code}")
async def signup(request: Request, db: DB, code: str, username: str = Form(), password: str = Form(),
                 password2: str = Form()):
    inv = _open_invite(db, code)
    username = username.strip().lower()
    error = None
    if inv is None:
        error = "Este convite não vale mais. Peça um novo para quem te convidou."
    elif not _USERNAME.match(username):
        error = "Usuário: 3 a 32 letras minúsculas, números, ponto, hífen ou sublinhado"
    elif db.scalar(select(User).where(User.username == username)):
        error = "Esse usuário já existe; escolha outro"
    elif len(password) < 8 or password != password2:
        error = "A senha precisa ter 8+ caracteres e as duas precisam ser iguais"
    if error:
        return render(request, "signup.html", invite=inv, code=code, error=error, username=username)
    user = User(username=username, password_hash=await asyncio.to_thread(hash_password, password))
    db.add(user)
    db.flush()
    inv.used_by, inv.used_at = user.id, utcnow()
    db.commit()
    request.session.clear()
    request.session["uid"] = user.id
    flash(request, f"Bem-vindo(a), {username}! Conecte o Telegram para receber os avisos.")
    return back("/telegram")


# ---------- Mercado Livre (OAuth) ----------

@app.get("/ml/connect", dependencies=[AdminOnly])
def ml_connect(request: Request):
    if not mercadolivre.configured():
        flash(request, "Preencha ML_CLIENT_ID e ML_CLIENT_SECRET no .env e reinicie o app", "err")
        return back("/telegram")
    verifier, challenge = mercadolivre.new_pkce()
    state = secrets.token_urlsafe(16)
    request.session["ml_oauth"] = {"state": state, "verifier": verifier}
    return RedirectResponse(mercadolivre.authorize_url(f"{_base(request)}/ml/callback", state, challenge))


@app.get("/ml/callback", dependencies=[AdminOnly])
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


@app.post("/ml/disconnect", dependencies=[AdminOnly])
def ml_disconnect(request: Request):
    mercadolivre.disconnect()
    flash(request, "Mercado Livre desconectado")
    return back("/telegram#ml")


@app.post("/coletor/token", dependencies=[AdminOnly])
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


@app.post("/telegram/phone", dependencies=[AdminOnly])
async def tg_phone(request: Request, phone: str = Form()):
    phone = "+" + "".join(c for c in phone if c.isdigit())
    return await _pairing_step(request, manager.send_code(phone))


@app.post("/telegram/code", dependencies=[AdminOnly])
async def tg_code(request: Request, code: str = Form()):
    return await _pairing_step(request, manager.sign_in_code(code))


@app.post("/telegram/password", dependencies=[AdminOnly])
async def tg_password(request: Request, password: str = Form()):
    return await _pairing_step(request, manager.sign_in_password(password))


@app.post("/telegram/logout", dependencies=[AdminOnly])
async def tg_logout(request: Request):
    return await _pairing_step(request, manager.logout())
