import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import notifier, scheduler
from .config import get_settings
from .db import get_db, init_db
from .models import Alert, Rule, Store, TgGroup, User
from .security import verify_password
from .telegram import PairingError, manager

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("telethon").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)  # a URL do bot contém o token
logging.getLogger("apscheduler").setLevel(logging.WARNING)

BASE = Path(__file__).parent
settings = get_settings()
templates = Jinja2Templates(directory=BASE / "templates")

READ_MODES = {"http": "HTTP (httpx + BeautifulSoup)", "browser": "Navegador (Playwright)"}


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

DB = Annotated[Session, Depends(get_db)]


# ---------- autenticação ----------

class LoginRequired(Exception):
    pass


@app.exception_handler(LoginRequired)
async def _to_login(request: Request, _: LoginRequired):
    if request.headers.get("HX-Request"):
        return Response(status_code=401, headers={"HX-Redirect": "/login"})
    return RedirectResponse("/login", status_code=303)


def require_user(request: Request) -> int:
    uid = request.session.get("uid")
    if not uid:
        raise LoginRequired
    return uid


Auth = Depends(require_user)


def render(request: Request, name: str, **ctx) -> HTMLResponse:
    ctx.setdefault("flash", request.session.pop("flash", None))
    ctx.setdefault("tg", manager)
    return templates.TemplateResponse(request, name, ctx)


def flash(request: Request, msg: str, kind: str = "ok") -> None:
    request.session["flash"] = {"msg": msg, "kind": kind}


def back(url: str) -> RedirectResponse:
    return RedirectResponse(url, status_code=303)


@app.get("/login")
def login_form(request: Request):
    return render(request, "login.html")


@app.post("/login")
async def login(request: Request, db: DB, username: str = Form(), password: str = Form()):
    user = db.scalar(select(User).where(User.username == username.strip()))
    if user and await asyncio.to_thread(verify_password, password, user.password_hash):
        request.session.clear()
        request.session["uid"] = user.id
        return back("/")
    await asyncio.sleep(1.5)  # freia tentativas de força bruta
    return render(request, "login.html", error="Usuário ou senha inválidos")


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return back("/login")


# ---------- PWA ----------

@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(BASE / "static/manifest.webmanifest", media_type="application/manifest+json")


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
    ok = await notifier.send("✅ price-watch: teste de aviso")
    if ok:
        flash(request, "Mensagem de teste enviada")
    else:
        hint = {"Unauthorized": "BOT_TOKEN inválido",
                "Bad Request: chat not found": "CHAT_ID errado ou você não tocou em Iniciar no bot",
                "Forbidden: bot was blocked by the user": "o bot está bloqueado; desbloqueie e toque em Iniciar"}
        why = notifier.last_error or "veja BOT_TOKEN/CHAT_ID"
        flash(request, f"Falha ao enviar: {hint.get(why, why)}", "err")
    return back("/alerts")


# ---------- lojas ----------

def clean_domain(raw: str) -> str:
    raw = raw.strip().lower()
    host = urlsplit(raw if "//" in raw else f"//{raw}").hostname or ""
    return host.removeprefix("www.")


def _store_form(name: str, domain: str, interval_minutes: int, read_mode: str) -> dict | str:
    domain = clean_domain(domain)
    if not name.strip() or not domain or "." not in domain:
        return "Informe nome e um domínio válido (ex.: loja.com.br)"
    if read_mode not in READ_MODES:
        return "Modo de leitura inválido"
    return {"name": name.strip(), "domain": domain,
            "interval_minutes": max(5, min(interval_minutes, 7 * 24 * 60)), "read_mode": read_mode}


@app.get("/stores", dependencies=[Auth])
def stores(request: Request, db: DB):
    items = db.scalars(select(Store).order_by(Store.name)).all()
    return render(request, "stores.html", stores=items, modes=READ_MODES, edit=None)


@app.get("/stores/{store_id}", dependencies=[Auth])
def store_edit(request: Request, db: DB, store_id: int):
    items = db.scalars(select(Store).order_by(Store.name)).all()
    return render(request, "stores.html", stores=items, modes=READ_MODES, edit=db.get(Store, store_id))


@app.post("/stores", dependencies=[Auth])
@app.post("/stores/{store_id}", dependencies=[Auth])
def store_save(request: Request, db: DB, name: str = Form(), domain: str = Form(),
               interval_minutes: int = Form(60), read_mode: str = Form("http"),
               store_id: int | None = None):
    data = _store_form(name, domain, interval_minutes, read_mode)
    if isinstance(data, str):
        flash(request, data, "err")
        return back("/stores")
    dup = db.scalar(select(Store).where(Store.domain == data["domain"]))
    if dup and dup.id != store_id:
        flash(request, "Já existe uma loja com esse domínio", "err")
        return back("/stores")
    store = db.get(Store, store_id) if store_id else Store()
    if store is None:
        return back("/stores")
    for k, v in data.items():
        setattr(store, k, v)
    db.add(store)
    db.commit()
    flash(request, "Loja salva")
    return back("/stores")


@app.post("/stores/{store_id}/delete", dependencies=[Auth])
def store_delete(request: Request, db: DB, store_id: int):
    if store := db.get(Store, store_id):
        db.delete(store)
        db.commit()
        flash(request, "Loja removida")
    return back("/stores")


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

@app.get("/telegram", dependencies=[Auth])
def telegram_page(request: Request):
    return render(request, "telegram.html", configured=settings.telegram_configured)


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
