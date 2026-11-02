"""Zoom (zoom.com.br): comparador que reúne preços de várias lojas.

O Zoom atende o servidor normalmente. Para cada produto acompanhado guardamos a página
do produto no Zoom (ou, se não houver, uma busca) e, a cada rodada, lemos as ofertas de
lá: cada loja vira uma oferta do produto marcada "via Zoom", que conta para o melhor
preço e para os avisos. Inclui lojas que bloqueiam leitura direta (ex.: Magalu)."""

import json
import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import parse_qs, quote, urlsplit

import httpx
from sqlalchemy import select

from .fetcher import HEADERS
from .item_rules import keyword_for, match_keyword_for
from .models import Item, Product, Store, utcnow
from .pricing import Extracted, pack_qty, parse_brl
from .rules import _keyword_found, normalize

log = logging.getLogger(__name__)
BASE = "https://www.zoom.com.br"
REFRESH_EVERY = timedelta(minutes=60)
transport: httpx.AsyncBaseTransport | None = None  # para testes

_NOT_NEW = re.compile(r"\b(seminovo|usado|recondicionado|vitrine|outlet|reembalado)\b")

# Nome da loja no Zoom -> domínio do nosso catálogo (para juntar com ofertas diretas)
SELLER_DOMAINS = {
    "amazon": "amazon.com.br", "magazine luiza": "magazineluiza.com.br", "magalu": "magazineluiza.com.br",
    "kabum": "kabum.com.br", "fast shop": "fastshop.com.br", "casas bahia": "casasbahia.com.br",
    "ponto": "pontofrio.com.br", "ponto frio": "pontofrio.com.br", "carrefour": "carrefour.com.br",
    "americanas": "americanas.com.br", "submarino": "submarino.com.br", "mercado livre": "mercadolivre.com.br",
    "shopee": "shopee.com.br", "netshoes": "netshoes.com.br", "pichau": "pichau.com.br",
    "terabyte": "terabyteshop.com.br", "terabyteshop": "terabyteshop.com.br", "leroy merlin": "leroymerlin.com.br",
    "aliexpress": "aliexpress.com",
}


@dataclass
class ZoomOffer:
    oid: str
    seller: str
    title: str
    price: int
    image: str | None = None


def _state(html: str) -> dict | None:
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))["props"]["initialReduxState"]
    except (ValueError, KeyError, TypeError):
        return None


def is_new(title: str) -> bool:
    return not _NOT_NEW.search(normalize(title or ""))


def parse_search(html: str) -> tuple[list[dict], list[ZoomOffer]]:
    """(produtos [{name, url, price}], ofertas soltas) da página de busca."""
    st = _state(html) or {}
    hits = ((st.get("hits") or {}).get("hits")) or []
    products, offers = [], []
    for h in hits:
        name, url, price = h.get("name") or "", h.get("url") or "", parse_brl(h.get("price"))
        if h.get("type") == "product" and url.startswith("/"):
            products.append({"name": name, "url": BASE + url, "price": price})
        elif h.get("type") == "offer" and price and is_new(name):
            oid = str((h.get("bestOffer") or {}).get("id") or url.rpartition("oid=")[2])
            offers.append(ZoomOffer(oid, h.get("merchantName") or "Loja", name, price, h.get("image")))
    return products, offers


def parse_product(html: str) -> list[ZoomOffer]:
    """Ofertas da página de produto (já ordenadas pelo menor preço)."""
    st = _state(html) or {}
    display = ((st.get("offers") or {}).get("displayOffers")) or []
    out = []
    for o in display:
        price = parse_brl(o.get("price"))
        if price and is_new(o.get("name") or "") and o.get("id"):
            out.append(ZoomOffer(str(o["id"]), o.get("sellerName") or "Loja", o.get("name") or "", price,
                                 o.get("imageUrl")))
    return out


def product_name(html: str) -> str | None:
    """Nome do produto na página de produto do Zoom (para conferir se é o certo)."""
    st = _state(html) or {}
    try:
        return st["products"]["product"]["product"]["name"] or None
    except (KeyError, TypeError):
        return None


def unlink(db, item: Item) -> None:
    """Desfaz o vínculo do produto com o Zoom e apaga as ofertas que vieram de lá."""
    item.zoom_url = None
    for offer in [o for o in item.offers if o.zoom_oid]:
        db.delete(offer)


def best_per_seller(offers: list[ZoomOffer]) -> list[ZoomOffer]:
    best: dict[str, ZoomOffer] = {}
    for o in offers:
        key = normalize(o.seller)
        if key not in best or o.price < best[key].price:
            best[key] = o
    return sorted(best.values(), key=lambda o: o.price)


async def _get(url: str) -> str | None:
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=25, transport=transport) as c:
        r = await c.get(url)
    return r.text if r.status_code == 200 else None


def _matches(item_name: str | None, brand: str | None, title: str, units: int = 1) -> bool:
    keyword = match_keyword_for(item_name, brand)
    return (bool(keyword and _keyword_found(keyword, normalize(title)))
            and pack_qty(title) == units)  # caixa com 12 não é a unidade avulsa


def _queries(item: Item, keyword: str) -> list[str]:
    terms = keyword.split(" + ")
    brand = set(re.findall(r"[a-z0-9]+", normalize(item.brand or "")))
    out = [" ".join(terms), " ".join(t for t in terms if t not in brand), " ".join(terms[:2])]
    return [q for q in dict.fromkeys(out) if q]


async def find(item: Item) -> tuple[str | None, str]:
    """Página do produto no Zoom (ou busca filtrada). Tenta termos completos, sem a
    marca e só os principais. Devolve (url, resumo)."""
    keyword = keyword_for(item.name, item.brand)
    if not keyword:
        return None, "Zoom: sem palavras para buscar"
    terms = set(keyword.split(" + "))
    fallback, seen = None, (0, 0)
    for q in _queries(item, keyword):
        search_url = f"{BASE}/search?q={quote(q)}"
        try:
            html = await _get(search_url)
        except httpx.HTTPError as e:
            return None, f"Zoom: sem resposta ({type(e).__name__})"
        if html is None:
            return None, "Zoom: busca recusada"
        products, offers = parse_search(html)
        seen = (seen[0] + len(products), seen[1] + len(offers))
        # Entre os que batem, o de nome mais "enxuto": evita pegar a versão Pro/Max/Plus
        matching = [p for p in products if _matches(item.name, item.brand, p["name"], item.units)]
        if matching:
            p = min(matching, key=lambda p: len(set(re.findall(r"[a-z0-9]+", normalize(p["name"]))) - terms))
            return p["url"], f"Zoom: produto encontrado ({p['name'][:60]})"
        hits = [o for o in offers if _matches(item.name, item.brand, o.title, item.units)]
        if hits and fallback is None:
            fallback = (search_url, len(best_per_seller(hits)))
    if fallback:
        return fallback[0], f"Zoom: {fallback[1]} loja(s) pela busca"
    return None, f"Zoom: {seen[0]} produto(s) e {seen[1]} oferta(s), nenhum igual"


def _store_for_seller(db, seller: str) -> Store:
    key = normalize(seller).replace("!", "").strip()
    domain = SELLER_DOMAINS.get(key) or "zoom-" + re.sub(r"[^a-z0-9]+", "-", key).strip("-")
    store = db.scalar(select(Store).where(Store.domain == domain))
    if store is None:
        store = Store(name=seller, domain=domain)
        db.add(store)
        db.flush()
    return store


async def refresh(item_id: int) -> list[tuple[int, Extracted]]:
    """Lê o Zoom e atualiza as ofertas "via Zoom" do produto. Devolve (oferta, leitura)
    para o monitor gravar preço/histórico e decidir avisos."""
    from .db import SessionLocal

    with SessionLocal() as db:
        item = db.get(Item, item_id)
        if item is None or not item.zoom_url:
            return []
        url, name, brand, units = item.zoom_url, item.name, item.brand, item.units
        item.zoom_checked_at = utcnow()
        db.commit()
    try:
        html = await _get(url)
    except httpx.HTTPError:
        return []
    if html is None:
        return []
    if "/search?" in url:
        keyword = keyword_for(name, brand)
        expected = _queries(Item(name=name, brand=brand), keyword) if keyword else []
        if parse_qs(urlsplit(url).query).get("q", [""])[0] not in expected:
            # Busca feita com palavras antigas: desfaz para a próxima leitura refazer
            with SessionLocal() as db:
                item = db.get(Item, item_id)
                unlink(db, item)
                item.discovered_at = None
                db.commit()
            return []
        offers = [o for o in parse_search(html)[1] if _matches(name, brand, o.title, units)]
    else:
        page_name = product_name(html)
        if page_name and not _matches(name, brand, page_name, units):
            # Vínculo errado (ex.: feito com palavras antigas): desfaz e apaga as ofertas de lá
            log.info("Zoom: página não corresponde ao produto %s; vínculo desfeito", item_id)
            with SessionLocal() as db:
                item = db.get(Item, item_id)
                unlink(db, item)
                item.discovered_at = None  # a próxima leitura refaz a busca
                db.commit()
            return []
        # a página do produto pode misturar unidade avulsa e caixa
        offers = [o for o in parse_product(html) if pack_qty(o.title) == units]
    offers = best_per_seller(offers)

    results = []
    with SessionLocal() as db:
        item = db.get(Item, item_id)
        direct = {o.store_id for o in item.offers if not o.zoom_oid and not o.last_error}
        current = {o.store_id: o for o in item.offers if o.zoom_oid}
        seen = set()
        for zo in offers:
            store = _store_for_seller(db, zo.seller)
            if store.id in direct:
                continue  # já lemos essa loja direto: fica a leitura direta
            seen.add(store.id)
            offer = current.get(store.id)
            lead = f"{BASE}/lead?oid={zo.oid}"
            if offer is None:
                if db.scalar(select(Product).where(Product.url == lead, Product.item_id == item_id)):
                    continue
                offer = Product(item=item, store=store, url=lead, zoom_oid=zo.oid, active=item.active)
                db.add(offer)
            else:
                offer.zoom_oid, offer.url = zo.oid, lead
            offer.name = offer.name or zo.title
            db.flush()
            results.append((offer.id, Extracted(price=zo.price, name=zo.title, image=zo.image, available=True)))
        for store_id, offer in current.items():
            if store_id not in seen:  # loja saiu do Zoom: deixa de contar no melhor preço
                offer.available = False
        db.commit()
    return results


def due_items() -> list[int]:
    from .db import SessionLocal

    now = utcnow()
    with SessionLocal() as db:
        items = db.scalars(select(Item).where(Item.active, Item.zoom_url.is_not(None))).all()
        due = []
        for i in items:
            checked = i.zoom_checked_at
            if checked is not None and checked.tzinfo is None:
                checked = checked.replace(tzinfo=now.tzinfo)
            if checked is None or checked + REFRESH_EVERY <= now:
                due.append(i.id)
        return due[:10]
