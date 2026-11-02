"""Telas de Produtos e Lojas (monitoramento de preços)."""

import asyncio

from datetime import timedelta
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, BackgroundTasks, Form, Request
from sqlalchemy import select

from . import affiliate, item_rules, linking, monitor
from .telegram import manager
from .models import Item, PricePoint, Product, Rule, Store, utcnow
from .pricing import find_url, host_of, parse_brl
from .stores_catalog import BY_DOMAIN, CATALOG
from .web import DB, Auth, back, flash, render

router = APIRouter(dependencies=[Auth])

READ_MODES = {"http": "HTTP", "browser": "Navegador"}
FETCH_VIA = {"vm": "Servidor", "home": "Celular de casa", "api": "API oficial (Mercado Livre)"}


# ---------- produtos (itens com ofertas em várias lojas) ----------

@router.get("/products")
def products(request: Request, db: DB, url: str = ""):
    items = db.scalars(select(Item).order_by(Item.active.desc(), Item.created_at.desc())).all()
    return render(request, "products.html", items=items, prefill=url)


@router.get("/share")
def share_target(title: str = "", text: str = "", url: str = ""):
    """Recebe o "Compartilhar" do Android (app instalado) e abre o cadastro."""
    link = find_url(url) or find_url(text) or find_url(title) or ""
    return back(f"/products?url={quote(link, safe='')}#novo")


async def _prepare_link(raw: str) -> tuple[str | None, str | None]:
    """(link limpo do produto, link original se era curto/de afiliado)."""
    link = find_url(raw)
    if not link or "." not in host_of(link):
        return None, None
    original = link if linking.needs_resolving(link) else None
    link = (await linking.resolve(link)).split("#")[0]  # links curtos e páginas de afiliado
    return affiliate.clean_url(link), original  # sem códigos de afiliado/rastreio de terceiros


def _start_reading(background: BackgroundTasks, offer: Product) -> None:
    if offer.store.fetch_via == "home":
        monitor.request_now(offer.id)
    else:
        background.add_task(monitor.check_product, offer.id)  # primeira consulta após responder


@router.post("/products")
async def product_add(request: Request, db: DB, background: BackgroundTasks, url: str = Form(),
                      target: str = Form(""), name: str = Form("")):
    link, original = await _prepare_link(url)
    if not link:
        flash(request, "Cole o link completo do produto (começando com https://)", "err")
        return back("/products")
    if existing := db.scalar(select(Product).where(Product.url == link)):
        flash(request, "Esse produto já está na lista", "err")
        return back(f"/products/{existing.item_id}" if existing.item_id else "/products")
    store = linking.store_for(db, link)
    item = Item(name=name.strip() or None, target_cents=parse_brl(target))
    offer = Product(item=item, store=store, url=link, source_url=original)
    db.add_all([item, offer])
    db.flush()
    rules_changed = item_rules.sync(db, item)  # sem nome ainda: a regra nasce na 1ª leitura
    db.commit()
    if rules_changed:
        await asyncio.to_thread(manager.reload)
    _start_reading(background, offer)
    flash(request, f"Produto adicionado ({store.name}). Consultando o preço…")
    return back("/products")


@router.post("/products/{item_id}/offers")
async def offer_add(request: Request, db: DB, background: BackgroundTasks, item_id: int, url: str = Form()):
    item = db.get(Item, item_id)
    if item is None:
        return back("/products")
    link, original = await _prepare_link(url)
    if not link:
        flash(request, "Cole o link completo do produto nessa outra loja", "err")
        return back(f"/products/{item_id}")
    if db.scalar(select(Product).where(Product.url == link)):
        flash(request, "Esse link já está cadastrado", "err")
        return back(f"/products/{item_id}")
    store = linking.store_for(db, link)
    offer = Product(item=item, store=store, url=link, source_url=original, active=item.active)
    db.add(offer)
    db.commit()
    _start_reading(background, offer)
    flash(request, f"Oferta da {store.name} adicionada. Consultando o preço…")
    return back(f"/products/{item_id}")


def _best_series(points: list[PricePoint]) -> list[int]:
    """Melhor preço ao longo do tempo, juntando o histórico de todas as lojas."""
    latest: dict[int, int] = {}
    series = []
    for pt in points:  # já em ordem de data
        latest[pt.product_id] = pt.cents
        series.append(min(latest.values()))
    return series


def _chart(vals: list[int], w: int = 320, h: int = 110) -> dict | None:
    """Coordenadas de um gráfico de linha simples (SVG) do histórico."""
    if len(vals) < 2:
        return None
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    pad = 8
    step = (w - 2 * pad) / (len(vals) - 1)
    xy = [(pad + i * step, pad + (h - 2 * pad) * (1 - (v - lo) / span)) for i, v in enumerate(vals)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in xy)
    area = f"{pad},{h - pad} {line} {w - pad},{h - pad}"
    return {"w": w, "h": h, "line": line, "area": area, "lo": lo, "hi": hi, "last": xy[-1]}


@router.get("/products/{item_id}")
def product_detail(request: Request, db: DB, item_id: int):
    item = db.get(Item, item_id)
    if item is None:
        return back("/products")
    since = utcnow() - timedelta(days=180)
    ids = [o.id for o in item.offers]
    points = db.scalars(select(PricePoint).where(PricePoint.product_id.in_(ids), PricePoint.checked_at >= since)
                        .order_by(PricePoint.checked_at)).all() if ids else []
    offers = sorted(item.offers, key=lambda o: (o.current_cents is None or bool(o.last_error)
                                                or o.available is False, o.current_cents or 0))
    rule = db.scalar(select(Rule).where(Rule.item_id == item.id))
    return render(request, "product_detail.html", item=item, offers=offers, best=item.best_offer(), rule=rule,
                  points=list(reversed(points[-30:])), chart=_chart(_best_series(points)))


@router.post("/products/{item_id}")
async def product_save(request: Request, db: DB, item_id: int, name: str = Form(""), target: str = Form(""),
                       active: bool = Form(False), watch_telegram: bool = Form(False)):
    item = db.get(Item, item_id)
    if item is None:
        return back("/products")
    item.name = name.strip() or item.name
    new_target = parse_brl(target)
    if new_target != item.target_cents:
        item.last_notified_cents = None  # alvo novo: volta a avisar
    item.target_cents = new_target
    item.active = active
    item.watch_telegram = watch_telegram
    for offer in item.offers:
        offer.active = active
    rules_changed = item_rules.sync(db, item)
    db.commit()
    if rules_changed:
        await asyncio.to_thread(manager.reload)
    flash(request, "Produto salvo")
    return back(f"/products/{item_id}")


@router.post("/products/{item_id}/check")
async def product_check(request: Request, db: DB, item_id: int):
    item = db.get(Item, item_id)
    if item:
        for offer_id, via in [(o.id, o.store.fetch_via) for o in item.offers]:
            if via == "home":
                monitor.request_now(offer_id)  # o coletor pega na próxima rodada (até 1 min)
            else:
                await monitor.check_product(offer_id)
    db.expire_all()
    item = db.get(Item, item_id)
    if request.headers.get("HX-Request") and item:
        return render(request, "_product_card.html", item=item)
    return back(f"/products/{item_id}" if item else "/products")


@router.post("/products/{item_id}/delete")
async def product_delete(request: Request, db: DB, item_id: int):
    if item := db.get(Item, item_id):
        db.delete(item)  # a regra do Telegram do produto vai junto (ON DELETE CASCADE)
        db.commit()
        await asyncio.to_thread(manager.reload)
        flash(request, "Produto removido")
    return back("/products")


@router.post("/offers/{offer_id}/delete")
def offer_delete(request: Request, db: DB, offer_id: int):
    offer = db.get(Product, offer_id)
    if offer is None:
        return back("/products")
    item_id = offer.item_id
    db.delete(offer)
    db.flush()
    item = db.get(Item, item_id) if item_id else None
    if item is not None:
        db.refresh(item)
        best = item.best_offer()
        item.best_cents = best.current_cents if best else None
    db.commit()
    flash(request, "Oferta removida")
    return back(f"/products/{item_id}" if item else "/products")


# ---------- lojas ----------

def clean_domain(raw: str) -> str:
    raw = raw.strip().lower()
    host = urlsplit(raw if "//" in raw else f"//{raw}").hostname or ""
    return host.removeprefix("www.")


def _stores_page(request: Request, db, edit: Store | None = None):
    items = db.scalars(select(Store).order_by(Store.name)).all()
    have = {s.domain for s in items}
    suggestions = [c for c in CATALOG if c["domain"] not in have]
    return render(request, "stores.html", stores=items, modes=READ_MODES, vias=FETCH_VIA, edit=edit,
                  suggestions=suggestions)


@router.get("/stores")
def stores(request: Request, db: DB):
    return _stores_page(request, db)


@router.get("/stores/{store_id}")
def store_edit(request: Request, db: DB, store_id: int):
    return _stores_page(request, db, db.get(Store, store_id))


@router.post("/stores/suggested")
def store_add_suggested(request: Request, db: DB, domain: str = Form()):
    c = BY_DOMAIN.get(domain)
    if c and not db.scalar(select(Store).where(Store.domain == domain)):
        db.add(Store(name=c["name"], domain=domain, read_mode=c["read_mode"], fetch_via=c["via"]))
        db.commit()
        flash(request, f"{c['name']} adicionada")
    return back("/stores")


@router.post("/stores")
@router.post("/stores/{store_id}")
def store_save(request: Request, db: DB, name: str = Form(), domain: str = Form(),
               interval_minutes: int = Form(60), read_mode: str = Form("http"),
               price_selector: str = Form(""), fetch_via: str = Form("vm"), store_id: int | None = None):
    domain = clean_domain(domain)
    if not name.strip() or not domain or "." not in domain:
        flash(request, "Informe nome e um domínio válido (ex.: loja.com.br)", "err")
        return back("/stores")
    dup = db.scalar(select(Store).where(Store.domain == domain))
    if dup and dup.id != store_id:
        flash(request, "Já existe uma loja com esse domínio", "err")
        return back("/stores")
    store = db.get(Store, store_id) if store_id else Store()
    if store is None:
        return back("/stores")
    store.name = name.strip()
    store.domain = domain
    store.interval_minutes = max(5, min(interval_minutes, 7 * 24 * 60))
    store.read_mode = read_mode if read_mode in READ_MODES else "http"
    store.price_selector = price_selector.strip() or None
    store.fetch_via = fetch_via if fetch_via in FETCH_VIA else "vm"
    db.add(store)
    db.commit()
    flash(request, "Loja salva")
    return back("/stores")


@router.post("/stores/{store_id}/delete")
def store_delete(request: Request, db: DB, store_id: int):
    if store := db.get(Store, store_id):
        db.delete(store)
        db.commit()
        flash(request, "Loja removida (e os produtos dela)")
    return back("/stores")


