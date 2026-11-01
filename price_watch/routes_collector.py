"""API do coletor de casa (autenticada por token, não pela sessão do painel)."""

import hmac
import secrets
import zlib

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse

from . import affiliate, monitor
from .db import SessionLocal
from .models import Product, Setting
from .web import BASE

router = APIRouter()
MAX_BODY = 12 << 20  # 12 MB descompactado


def collector_token(regenerate: bool = False) -> str:
    with SessionLocal() as db:
        row = db.get(Setting, "collector_token")
        if row is None or regenerate:
            value = secrets.token_urlsafe(24)
            if row is None:
                db.add(Setting(key="collector_token", value=value))
            else:
                row.value = value
            db.commit()
            return value
        return row.value


def _auth(request: Request) -> None:
    sent = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not sent or not hmac.compare_digest(sent, collector_token()):
        raise HTTPException(status_code=401)


@router.get("/coletor.py", include_in_schema=False)
def collector_script():
    # Público de propósito: o script não tem segredos (o token vai no comando)
    return FileResponse(BASE / "static/coletor.py", media_type="text/x-python",
                        headers={"Cache-Control": "no-cache"})


@router.get("/api/coletor/jobs")
def jobs(request: Request):
    _auth(request)
    return {"jobs": monitor.collector_jobs()}


@router.post("/api/coletor/result")
async def result(request: Request, id: int, status: int = 0, error: str = ""):
    _auth(request)
    raw = await request.body()
    try:  # descompacta com limite (evita estourar a memória da VM)
        data = zlib.decompressobj(16 + zlib.MAX_WBITS).decompress(raw, MAX_BODY + 1) if raw else b""
    except zlib.error:
        raise HTTPException(status_code=400)
    if len(data) > MAX_BODY:
        raise HTTPException(status_code=413)
    html = data.decode("utf-8", errors="replace") if data and not error else None
    await monitor.process_collected(id, status, html, error[:200] or None)
    return {"ok": True}


@router.get("/r/{product_id}", include_in_schema=False)
def buy(product_id: int, s: str = ""):
    """Botão "Comprar": conta o clique e leva ao link com o código de afiliado do app."""
    if not hmac.compare_digest(s, affiliate.click_sig(product_id)):
        raise HTTPException(status_code=404)
    with SessionLocal() as db:
        p = db.get(Product, product_id)
        if p is None:
            raise HTTPException(status_code=404)
        p.clicks = (p.clicks or 0) + 1
        db.commit()
        target = affiliate.buy_url(p.url)
    return RedirectResponse(target, status_code=302, headers={"Referrer-Policy": "no-referrer"})
