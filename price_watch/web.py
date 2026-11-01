"""Auxiliares compartilhados pelas rotas: templates, sessão e autenticação."""

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from zoneinfo import ZoneInfo

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db
from .affiliate import click_sig
from .pricing import format_brl
from .telegram import manager

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=BASE / "templates")
DB = Annotated[Session, Depends(get_db)]


def _ago(dt: datetime | None) -> str:
    if dt is None:
        return "nunca"
    if dt.tzinfo is None:  # SQLite devolve sem fuso
        dt = dt.replace(tzinfo=UTC)
    s = int((datetime.now(UTC) - dt).total_seconds())
    if s < 60:
        return "agora"
    if s < 3600:
        return f"há {s // 60} min"
    if s < 86400:
        return f"há {s // 3600} h"
    return f"há {s // 86400} d"


_TZ = ZoneInfo(get_settings().TIMEZONE)


def _local(dt: datetime | None, fmt: str = "%d/%m %H:%M") -> str:
    """Data no fuso do usuário (o banco guarda em UTC, às vezes sem fuso)."""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(_TZ).strftime(fmt)


templates.env.filters["local"] = _local
templates.env.globals["buy_link"] = lambda p: f"/r/{p.id}?s={click_sig(p.id)}"
templates.env.filters["brl"] = format_brl
templates.env.filters["ago"] = _ago


class LoginRequired(Exception):
    pass


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
