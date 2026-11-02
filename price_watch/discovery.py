"""Busca automática do mesmo produto em outras lojas.

Fontes (todas atendem o servidor normalmente): Americanas (busca pública VTEX, com
EAN), KaBuM (dados da página de busca) e Mercado Livre (catálogo da API oficial).
EAN ou código de modelo iguais => oferta adicionada; só o nome => sugestão."""

import json
import logging
import re
from dataclasses import dataclass
from urllib.parse import quote

import httpx
from sqlalchemy import select

from . import affiliate, linking, mercadolivre
from .db import SessionLocal
from .fetcher import HEADERS
from .item_rules import keyword_for, match_keyword_for
from .models import Item, Product, Suggestion, utcnow
from .pricing import normalize_gtin, parse_brl
from .rules import _keyword_found, normalize

log = logging.getLogger(__name__)
transport: httpx.AsyncBaseTransport | None = None  # para testes
MAX_CANDIDATES = 5


class SearchError(Exception):
    """Falha da busca numa loja (mensagem curta para o relatório)."""


@dataclass
class Candidate:
    store_name: str
    url: str
    title: str
    price: int | None = None
    gtin: str | None = None


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=20, transport=transport)


# ---------- buscas por loja ----------

async def search_americanas(query: str | None = None, ean: str | None = None) -> list[Candidate]:
    # A VTEX recusa espaço codificado como "+" (HTTP 400): monta a URL com %20
    filtro = f"fq=alternateIds_Ean:{ean}" if ean else f"ft={quote(query or '')}"
    url = ("https://www.americanas.com.br/api/catalog_system/pub/products/search"
           f"?{filtro}&_from=0&_to={MAX_CANDIDATES - 1}")
    async with _client() as c:
        r = await c.get(url)
    if r.status_code not in (200, 206):
        raise SearchError(f"a loja recusou a busca (HTTP {r.status_code})")
    out = []
    for p in r.json() or []:
        item = (p.get("items") or [{}])[0]
        offer = ((item.get("sellers") or [{}])[0]).get("commertialOffer") or {}
        if not p.get("link") or not offer.get("AvailableQuantity", 1):
            continue
        out.append(Candidate("Americanas", p["link"].split("?")[0], p.get("productName") or "",
                             parse_brl(offer.get("Price")), normalize_gtin(item.get("ean"))))
    return out


def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


async def search_kabum(query: str | None = None, ean: str | None = None) -> list[Candidate]:
    if not query:
        return []
    slug = quote(re.sub(r"\s+", "-", normalize(query)))
    async with _client() as c:
        r = await c.get(f"https://www.kabum.com.br/busca/{slug}")
    if r.status_code != 200:
        raise SearchError(f"a loja recusou a busca (HTTP {r.status_code})")
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.S)
    if not m:
        raise SearchError("página de busca sem dados (formato mudou?)")
    try:
        data = json.loads(m.group(1))
    except ValueError:
        return []
    out, seen = [], set()
    for d in _walk(data):
        code, friendly, name = d.get("code"), d.get("friendlyName"), d.get("name")
        if not (isinstance(code, int) and isinstance(friendly, str) and isinstance(name, str)) or code in seen:
            continue
        seen.add(code)
        price = parse_brl(d.get("priceWithDiscount") or d.get("price"))
        out.append(Candidate("KaBuM!", f"https://www.kabum.com.br/produto/{code}/{friendly}", name, price))
        if len(out) >= MAX_CANDIDATES:
            break
    return out


async def search_mercadolivre(query: str | None = None, ean: str | None = None) -> list[Candidate]:
    if not mercadolivre.configured() or not mercadolivre.account():
        raise SearchError("conta do Mercado Livre não conectada")
    if not ean:
        raise SearchError("só busca por EAN, e este produto ainda não tem EAN")
    try:
        token = await mercadolivre._access_token()
    except Exception as e:  # noqa: BLE001
        raise SearchError(str(e)[:80]) from e
    async with mercadolivre._client() as c:
        r = await c.get("/products/search", headers={"Authorization": f"Bearer {token}"},
                        params={"status": "active", "site_id": "MLB", "product_identifier": ean})
    if r.status_code != 200:
        raise SearchError(f"a API recusou a busca (HTTP {r.status_code})")
    return [Candidate("Mercado Livre", f"https://www.mercadolivre.com.br/p/{p['id']}", p.get("name") or "",
                      gtin=ean) for p in (r.json().get("results") or [])[:2] if p.get("id")]


ZOOM_ENABLED = True

SEARCHERS = {
    "americanas.com.br": search_americanas,
    "kabum.com.br": search_kabum,
    "mercadolivre.com.br": search_mercadolivre,
}


# ---------- comparação ----------

def _alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", normalize(text or ""))


def match_kind(item: Item, cand: Candidate) -> str | None:
    """'ean' (certeza), 'modelo' (código do fabricante no título), 'nome' (todas as
    palavras-chave do produto no título) ou None."""
    if item.gtin and cand.gtin and normalize_gtin(item.gtin) == normalize_gtin(cand.gtin):
        return "ean"
    model = _alnum(item.model or "")
    if len(model) >= 5 and re.search(r"\d", model) and re.search(r"[a-z]", model) and model in _alnum(cand.title):
        return "modelo"
    keyword = match_keyword_for(item.name, item.brand)
    if keyword and _keyword_found(keyword, normalize(cand.title)):
        return "nome"
    return None


def search_queries(item: Item) -> list[str]:
    """Termos de busca, do mais completo ao mais curto (algumas lojas tratam várias
    palavras como "qualquer uma" e trazem produtos parecidos; com menos palavras acertam)."""
    out = []
    if item.brand and item.model:
        out.append(f"{item.brand} {item.model}")
    keyword = keyword_for(item.name, item.brand)
    if keyword:
        terms = keyword.split(" + ")
        out.append(" ".join(terms))
        if len(terms) > 2:
            out.append(" ".join(terms[:2]))
    return list(dict.fromkeys(out))


def search_query(item: Item) -> str | None:
    queries = search_queries(item)
    return queries[0] if queries else None


# ---------- orquestração ----------

def _snapshot(item_id: int):
    with SessionLocal() as db:
        item = db.get(Item, item_id)
        if item is None or not item.name:
            return None
        item.discovered_at = utcnow()  # marca já: evita buscas repetidas em paralelo
        have = {o.store.domain for o in item.offers}
        snap = Item(id=item.id, name=item.name, brand=item.brand, model=item.model, gtin=item.gtin)
        db.commit()
        return snap, have


async def discover(item_id: int) -> dict:
    """Procura o produto nas lojas que ainda não têm oferta dele e no Zoom. Guarda um
    relatório por loja na tela do produto. Devolve um resumo."""
    from . import zoom

    snap = _snapshot(item_id)
    summary = {"added": [], "suggested": 0, "zoom": False}
    if snap is None:
        return summary
    item, have = snap
    queries = search_queries(item)
    report = [f"Termos da busca: {' / '.join(queries) or '—'}" + (f" · EAN {item.gtin}" if item.gtin else "")]
    for domain, search in SEARCHERS.items():
        label = domain.split(".")[0].capitalize()
        if domain in have:
            report.append(f"{label}: já acompanhado")
            continue
        cands, scored, failure = [], [], None
        attempts = ([{"ean": item.gtin}] if item.gtin else []) + [{"query": q} for q in queries]
        for args in attempts:  # para na primeira tentativa que trouxer algo que bate
            try:
                found = await search(**args)
            except SearchError as e:
                failure = str(e)
                continue
            except (httpx.HTTPError, ValueError) as e:
                failure = f"sem resposta ({type(e).__name__})"
                continue
            cands += found
            scored = [(match_kind(item, c), c) for c in cands]
            if any(kind for kind, _ in scored):
                break
        if not cands and failure:
            report.append(f"{label}: {failure}")
            continue
        sure = next((c for kind, c in scored if kind in ("ean", "modelo")), None)
        if sure:
            if _add_offer(item_id, sure):
                summary["added"].append(sure.store_name)
                report.append(f"{label}: encontrado pelo {next(k for k, c in scored if c is sure)} ✓")
            else:
                report.append(f"{label}: encontrado, mas o link já estava cadastrado")
            continue
        names = [c for kind, c in scored if kind == "nome"][:2]
        n = _suggest(item_id, names)
        summary["suggested"] += n
        if n:
            report.append(f"{label}: {n} sugestão(ões) para confirmar")
        else:
            report.append(f"{label}: {len(cands)} resultado(s), nenhum com as palavras do produto")

    zoom_url, zoom_note = (None, "Zoom: desligado") if not ZOOM_ENABLED else await zoom.find(item)
    report.append(zoom_note)
    with SessionLocal() as db:
        it = db.get(Item, item_id)
        if it is not None:
            if zoom_url and not it.zoom_url:
                it.zoom_url = zoom_url
                summary["zoom"] = True
            elif it.zoom_url:
                summary["zoom"] = True
            it.discovery_report = "\n".join(report)
            db.commit()
    return summary


def _add_offer(item_id: int, cand: Candidate) -> int | None:
    url = affiliate.clean_url(cand.url)
    with SessionLocal() as db:
        item = db.get(Item, item_id)
        if item is None or db.scalar(select(Product).where(Product.url == url)):
            return None
        offer = Product(item=item, store=linking.store_for(db, url), url=url, active=item.active)
        db.add(offer)
        db.commit()
        _new_offers.append(offer.id)
        return offer.id


def _suggest(item_id: int, cands: list[Candidate]) -> int:
    n = 0
    with SessionLocal() as db:
        for c in cands:
            url = affiliate.clean_url(c.url)
            exists = db.scalar(select(Suggestion).where(Suggestion.item_id == item_id, Suggestion.url == url))
            if exists or db.scalar(select(Product).where(Product.url == url)):
                continue
            db.add(Suggestion(item_id=item_id, url=url, store_name=c.store_name, title=c.title[:300],
                              price_cents=c.price))
            n += 1
        db.commit()
    return n


# Ofertas criadas pela busca, para o monitor consultar logo em seguida
_new_offers: list[int] = []


def take_new_offers() -> list[int]:
    ids = list(_new_offers)
    _new_offers.clear()
    return ids


def needs_discovery(product_id: int) -> int | None:
    """Item da oferta que ainda não passou pela busca (e já tem nome)."""
    with SessionLocal() as db:
        p = db.get(Product, product_id)
        item = p.item if p else None
        if item and item.name and item.discovered_at is None and item.active:
            return item.id
        return None

