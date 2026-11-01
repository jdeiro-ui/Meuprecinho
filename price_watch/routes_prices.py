"""Telas de Produtos e Lojas (monitoramento de preços)."""

from datetime import timedelta
from urllib.parse import quote, urlsplit

from fastapi import APIRouter, BackgroundTasks, Form, Request
from sqlalchemy import select

from . import monitor
from .models import PricePoint, Product, Store, utcnow
from .pricing import find_url, host_of, match_store, parse_brl
from .stores_catalog import BY_DOMAIN, CATALOG
from .web import DB, Auth, back, flash, render

router = APIRouter(dependencies=[Auth])

READ_MODES = {"http": "HTTP", "browser": "Navegador"}
FETCH_VIA = {"vm": "Servidor", "home": "Celular de casa", "api": "API oficial (Mercado Livre)"}


# ---------- produtos ----------

@router.get("/products")
def products(request: Request, db: DB, url: str = ""):
    items = db.scalars(select(Product).order_by(Product.active.desc(), Product.created_at.desc())).all()
    return render(request, "products.html", products=items, prefill=url)


@router.get("/share")
def share_target(title: str = "", text: str = "", url: str = ""):
    """Recebe o "Compartilhar" do Android (app instalado) e abre o cadastro."""
    link = find_url(url) or find_url(text) or find_url(title) or ""
    return back(f"/products?url={quote(link, safe='')}#novo")


def _store_for(db, url: str) -> Store:
    host = host_of(url)
    stores = db.scalars(select(Store)).all()
    domain = match_store(host, [s.domain for s in stores])
    if domain:
        return next(s for s in stores if s.domain == domain)
    known = match_store(host, list(BY_DOMAIN))
    if known:
        c = BY_DOMAIN[known]
        store = Store(name=c["name"], domain=known, read_mode=c["read_mode"], fetch_via=c["via"])
    else:  # loja desconhecida: cria com o domínio como nome
        store = Store(name=host.split(".")[0].capitalize(), domain=host)
    db.add(store)
    db.flush()
    return store


@router.post("/products")
def product_add(request: Request, db: DB, background: BackgroundTasks, url: str = Form(),
                target: str = Form(""), name: str = Form("")):
    link = find_url(url)
    if not link or "." not in host_of(link):
        flash(request, "Cole o link completo do produto (começando com https://)", "err")
        return back("/products")
    link = link.split("#")[0]
    if db.scalar(select(Product).where(Product.url == link)):
        flash(request, "Esse produto já está na lista", "err")
        return back("/products")
    store = _store_for(db, link)
    p = Product(store=store, url=link, name=name.strip() or None, target_cents=parse_brl(target))
    db.add(p)
    db.commit()
    if store.fetch_via == "home":
        flash(request, f"Produto adicionado em {store.name}. O coletor de casa vai consultar o preço.")
    else:
        background.add_task(monitor.check_product, p.id)  # primeira consulta logo após responder
        flash(request, f"Produto adicionado em {store.name}. Consultando o preço…")
    return back("/products")


def _chart(points: list[PricePoint], w: int = 320, h: int = 110) -> dict | None:
    """Coordenadas de um gráfico de linha simples (SVG) do histórico."""
    if len(points) < 2:
        return None
    vals = [pt.cents for pt in points]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    pad = 8
    step = (w - 2 * pad) / (len(vals) - 1)
    xy = [(pad + i * step, pad + (h - 2 * pad) * (1 - (v - lo) / span)) for i, v in enumerate(vals)]
    line = " ".join(f"{x:.1f},{y:.1f}" for x, y in xy)
    area = f"{pad},{h - pad} {line} {w - pad},{h - pad}"
    return {"w": w, "h": h, "line": line, "area": area, "lo": lo, "hi": hi, "last": xy[-1]}


@router.get("/products/{product_id}")
def product_detail(request: Request, db: DB, product_id: int):
    p = db.get(Product, product_id)
    if p is None:
        return back("/products")
    since = utcnow() - timedelta(days=180)
    points = db.scalars(select(PricePoint).where(PricePoint.product_id == p.id, PricePoint.checked_at >= since)
                        .order_by(PricePoint.checked_at)).all()
    return render(request, "product_detail.html", p=p, points=list(reversed(points[-30:])), chart=_chart(points))


@router.post("/products/{product_id}")
def product_save(request: Request, db: DB, product_id: int, name: str = Form(""), target: str = Form(""),
                 active: bool = Form(False)):
    p = db.get(Product, product_id)
    if p is None:
        return back("/products")
    p.name = name.strip() or p.name
    new_target = parse_brl(target)
    if new_target != p.target_cents:
        p.last_notified_cents = None  # alvo novo: volta a avisar
    p.target_cents = new_target
    p.active = active
    db.commit()
    flash(request, "Produto salvo")
    return back(f"/products/{product_id}")


@router.post("/products/{product_id}/check")
async def product_check(request: Request, db: DB, product_id: int):
    p = db.get(Product, product_id)
    if p and p.store.fetch_via == "home":
        monitor.request_now(product_id)  # o coletor pega na próxima rodada (até 1 min)
    elif p:
        await monitor.check_product(product_id)
    db.expire_all()
    p = db.get(Product, product_id)
    if request.headers.get("HX-Request") and p:
        return render(request, "_product_card.html", p=p)
    return back(f"/products/{product_id}" if p else "/products")


@router.post("/products/{product_id}/delete")
def product_delete(request: Request, db: DB, product_id: int):
    if p := db.get(Product, product_id):
        db.delete(p)
        db.commit()
        flash(request, "Produto removido")
    return back("/products")


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


