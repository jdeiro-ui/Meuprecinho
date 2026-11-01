"""Links de produto: segue encurtadores (meli.la, amzn.to...) e escolhe a loja."""

import re
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from .fetcher import HEADERS
from .models import Store
from .pricing import host_of, match_store
from .stores_catalog import BY_DOMAIN

# Encurtadores comuns em links de afiliado e de grupos de promoção
SHORTENERS = {
    "meli.la", "mercadolivre.com", "amzn.to", "a.co", "amzn.com", "bit.ly", "tinyurl.com",
    "tidd.ly", "shope.ee", "s.shopee.com.br", "cutt.ly", "is.gd", "t.co", "rebrand.ly",
    "encurtador.com.br", "abre.ai", "onelink.to", "magalu.lu", "s.click.aliexpress.com",
}
_META_REFRESH = re.compile(r"""url=['"]?([^'" >]+)""", re.I)
# Página "social" do afiliado do Mercado Livre (destino do meli.la): o produto
# compartilhado vem no estado da página, ex.: "shared_item":{"id":"MLB123","type":"ITEM"}
_ML_SHARED = re.compile(r'"shared_item"\s*:\s*\{\s*"id"\s*:\s*"(MLB[A-Z]?\d+)"\s*,\s*"type"\s*:\s*"(\w+)"')
MAX_BODY = 3 << 20

transport: httpx.AsyncBaseTransport | None = None  # para testes


def is_short(url: str) -> bool:
    return host_of(url) in SHORTENERS


def is_ml_social(url: str) -> bool:
    host = host_of(url)
    return (host == "mercadolivre.com.br" or host.endswith(".mercadolivre.com.br")) \
        and urlsplit(url).path.startswith("/social/")


def needs_resolving(url: str) -> bool:
    return is_short(url) or is_ml_social(url)


def ml_canonical(ml_id: str, kind: str) -> str:
    if kind.upper() == "PRODUCT":
        return f"https://www.mercadolivre.com.br/p/{ml_id}"
    if ml_id.startswith("MLBU"):
        return f"https://www.mercadolivre.com.br/up/{ml_id}"
    return f"https://produto.mercadolivre.com.br/MLB-{ml_id[3:]}"


async def _read(r: httpx.Response) -> str:
    chunks, size = [], 0
    async for chunk in r.aiter_bytes():
        chunks.append(chunk)
        size += len(chunk)
        if size > MAX_BODY:
            break
    return b"".join(chunks).decode("utf-8", "replace")


async def resolve(url: str) -> str:
    """Endereço real do produto: segue links curtos e, na página social de afiliado
    do Mercado Livre, pega o produto compartilhado. Se falhar, devolve o próprio link."""
    if not needs_resolving(url):
        return url
    try:
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=20,
                                     transport=transport) as c:
            async with c.stream("GET", url) as r:
                final = str(r.url)
                if is_ml_social(final):
                    if m := _ML_SHARED.search(await _read(r)):
                        return ml_canonical(m.group(1), m.group(2))
                    return url
                if final == url or is_short(final):  # alguns usam redirecionamento por HTML
                    if m := _META_REFRESH.search((await _read(r))[:20000]):
                        final = m.group(1)
    except httpx.HTTPError:
        return url
    return final.split("#")[0] if final.startswith("http") else url


def store_for(db, url: str) -> Store:
    """Loja cadastrada que cobre o link; se não houver, cria (do catálogo ou pelo domínio)."""
    host = host_of(url)
    stores = db.scalars(select(Store)).all()
    domain = match_store(host, [s.domain for s in stores])
    if domain:
        return next(s for s in stores if s.domain == domain)
    known = match_store(host, list(BY_DOMAIN))
    if known:
        c = BY_DOMAIN[known]
        store = Store(name=c["name"], domain=known, read_mode=c["read_mode"], fetch_via=c["via"])
    else:
        store = Store(name=host.split(".")[0].capitalize(), domain=host)
    db.add(store)
    db.flush()
    return store


def ml_social_offer(html: str) -> dict | None:
    """Na página social de afiliado, dados do produto compartilhado:
    {"id", "price" (centavos), "name", "image"}; None se não achar."""
    from .pricing import parse_brl

    m = _ML_SHARED.search(html)
    if not m:
        return None
    ml_id = m.group(1)
    out = {"id": ml_id, "price": None, "name": None, "image": None}
    card = html.find(f'"metadata":{{"id":"{ml_id}"')
    if card >= 0:
        seg = html[card:card + 8000]
        if p := re.search(r'"current_price"\s*:\s*\{\s*"value"\s*:\s*([\d.]+)', seg):
            out["price"] = parse_brl(float(p.group(1)))
    for prop, key in (("og:title", "name"), ("og:image", "image")):
        if t := re.search(rf'<meta[^>]+property="{prop}"[^>]+content="([^"]+)"', html):
            out[key] = t.group(1)
    if out["name"]:
        name, sep, price = out["name"].rpartition(" - R$")
        if sep:
            out["name"] = name.strip()
            out["price"] = out["price"] or parse_brl(price)
    return out


async def ml_offer_from_link(link: str) -> dict | None:
    """Abre o link de afiliado (meli.la ou página social) e lê o produto compartilhado."""
    try:
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=25,
                                     transport=transport) as c:
            async with c.stream("GET", link) as r:
                if r.status_code != 200 or not is_ml_social(str(r.url)):
                    return None
                return ml_social_offer(await _read(r))
    except httpx.HTTPError:
        return None
