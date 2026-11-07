"""Aba Ofertas: feed das promoções dos canais monitorados."""

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, RedirectResponse, Response
from sqlalchemy import select

from . import item_rules, offers
from .models import Offer, Rule
from .telegram import manager
from .web import DB, Auth, Me, back, flash, render
from .pricing import format_brl

router = APIRouter(dependencies=[Auth])
PAGE = 40


@router.get("/ofertas")
def feed_page(request: Request, db: DB, me: Me, q: str = "", loja: str = "", p: int = 0):
    p = max(p, 0)
    items = offers.feed(db, q.strip(), loja, limit=PAGE + 1, offset=p * PAGE)
    return render(request, "ofertas.html", offers=items[:PAGE], more=len(items) > PAGE, page=p, q=q, loja=loja,
                  stores=offers.stores_in_feed(db))


@router.get("/ofertas/img/{offer_id}")
def offer_image(offer_id: int):
    path = offers.image_path(offer_id)
    if not path.is_file():
        return Response(status_code=404)
    return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=604800"})


@router.get("/o/{offer_id}")
def offer_go(db: DB, offer_id: int):
    """Abre a loja (e conta o clique). O endereço vem só do que o app guardou da mensagem."""
    o = db.get(Offer, offer_id)
    if o is None:
        return RedirectResponse("/ofertas", status_code=303)
    o.clicks += 1
    db.commit()
    return RedirectResponse(o.url, status_code=302)


@router.post("/ofertas/{offer_id}/seguir")
async def offer_follow(request: Request, db: DB, me: Me, offer_id: int):
    """Cria uma regra para avisar quando esse produto aparecer de novo (até o preço da oferta)."""
    o = db.get(Offer, offer_id)
    keyword = item_rules.keyword_for(o.title) if o else None
    if not keyword:
        flash(request, "Não consegui montar uma regra para essa oferta; crie a sua em Regras", "err")
        return back("/ofertas")
    if db.scalar(select(Rule).where(Rule.user_id == me.id, Rule.keyword == keyword)) is None:
        db.add(Rule(user_id=me.id, keyword=keyword, max_price_cents=o.price_cents))
        db.commit()
        await asyncio.to_thread(manager.reload)
    until = f" até {format_brl(o.price_cents)}" if o.price_cents else ""
    flash(request, f"Pronto: você será avisado quando aparecer{until}")
    return back("/ofertas")
