"""Links de produto: segue encurtadores (meli.la, amzn.to...) e escolhe a loja."""

import re

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

transport: httpx.AsyncBaseTransport | None = None  # para testes


def is_short(url: str) -> bool:
    return host_of(url) in SHORTENERS


async def resolve(url: str) -> str:
    """Endereço final de um link curto (ou o próprio link, se falhar)."""
    if not is_short(url):
        return url
    try:
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=15,
                                     transport=transport) as c:
            async with c.stream("GET", url) as r:
                final = str(r.url)
                if final == url or is_short(final):  # alguns usam redirecionamento por HTML
                    body = (await r.aread())[:20000].decode("utf-8", "replace")
                    if m := _META_REFRESH.search(body):
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
