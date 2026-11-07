"""Aba Ofertas: promoções dos canais monitorados, mostradas a todos os usuários.

Cada mensagem com link de loja e preço (ou cupom) vira um cartão: nome, preço, cupom, loja
e imagem. A mesma promoção em vários canais vira um cartão só. O nome do canal fica só
para o administrador."""

import logging
import re
from collections import Counter, deque
from dataclasses import dataclass
from datetime import timedelta
from urllib.parse import urlsplit

import httpx
from sqlalchemy import delete, or_, select

from . import affiliate, mercadolivre
from .config import get_settings
from .db import SessionLocal
from .models import Offer, utcnow
from .pricing import price_in_text
from .rules import normalize
from .stores_catalog import CATALOG

log = logging.getLogger(__name__)

RETENTION = timedelta(days=7)
MERGE_WINDOW = timedelta(hours=48)
GENERIC_SOURCE = "Oferta encontrada"

_URL = re.compile(r"https?://[^\s<>\]\)]+", re.I)
_SOCIAL = ("t.me", "telegram.me", "telegram.org", "instagram.com", "wa.me", "whatsapp.com",
           "facebook.com", "twitter.com", "x.com", "youtube.com", "youtu.be", "tiktok.com")
_SHORTENERS = ("bit.ly", "tinyurl.com", "cutt.ly", "is.gd", "t.ly", "shre.ly", "encurtador.com.br")
_ALIASES = {"amzn.to": "Amazon", "amzn.com": "Amazon", "a.co": "Amazon", "meli.la": "Mercado Livre",
            "mercadolibre.com": "Mercado Livre", "mercadolivre.com": "Mercado Livre",
            "magalu.com": "Magalu", "magazinevoce.com.br": "Magalu", "shope.ee": "Shopee",
            "shopee.com.br": "Shopee", "aliexpress.com": "AliExpress", "s.click.aliexpress.com": "AliExpress",
            "link.amazon": "Amazon", "tenis.cc": "Tênis Certo"}
_STORES = {s["domain"]: s["name"] for s in CATALOG} | _ALIASES

_PRICE_TOKEN = re.compile(r"R\$\s*\d[\d.,]*(?:\s*(?:à vista|no pix|no boleto|no cartão))?", re.I)
_COUPON = re.compile(r"(?i:cupom|c[óo]digo|cod\.?)[^\n\w]{0,18}[`\"“']?([A-Z0-9][A-Z0-9_-]{2,23})\b")
_COUPON_STOP = {"CUPOM", "CODIGO", "CÓDIGO", "DESCONTO", "APLICADO", "OFF", "FRETE", "GRATIS", "GRÁTIS"}
_COUPON_LINE = re.compile(r"(?i)\b(?:cupom|c[óo]digo)\b.*$")
_GENERIC = re.compile(r"(?i)^\W*(promo[cç][aã]o|oferta|imperd[ií]vel|corre|aproveite|achadinho|urgente|alerta|"
                      r"bug|r[ée]lampago|top|atenção|lan[cç]ou|saiu)\b")
_TRAIL = re.compile(r"(?i)\s+(?:por|de|apenas|a partir de|s[óo]|agora|saiu por)$")
_TG_REF = re.compile(r"(?i)(?:https?://)?(?:t\.me|telegram\.me)/\S+|@\w{3,}")


def scrub(text: str) -> str:
    """Tira de um texto links de canais do Telegram e @menções."""
    return " ".join(_TG_REF.sub("", text).split())


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _store_of(host: str) -> str | None:
    for dom, name in _STORES.items():
        if host == dom or host.endswith("." + dom):
            return name
    if any(host == d or host.endswith("." + d) for d in _SHORTENERS):
        return "Loja"
    return None


def _urls(text: str) -> list[str]:
    return list(dict.fromkeys(u.rstrip(".,;:!?)") for u in _URL.findall(text)))


def first_store_url(text: str) -> str | None:
    """Primeiro link de loja da mensagem (os links de canal/rede social ficam de fora)."""
    for u in _urls(text):
        h = _host(u)
        if not any(h == d or h.endswith("." + d) for d in _SOCIAL) and _store_of(h):
            return u
    return None


# Encurtadores de loja: seguimos o redirecionamento para saber a loja real e juntar duplicatas
_RESOLVE = ("link.amazon", "amzn.to", "amzn.com", "a.co", "tenis.cc", "meli.la", *_SHORTENERS)
_resolved: dict[str, str | None] = {}
_UA = "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Mobile Safari/537.36"


def _is_short(host: str) -> bool:
    return any(host == d or host.endswith("." + d) for d in _RESOLVE)


def resolve(url: str) -> str | None:
    """Destino final de um link encurtado (para ao chegar numa loja conhecida, sem abri-la)."""
    if url in _resolved:
        return _resolved[url]
    final = None
    try:
        with httpx.Client(timeout=4, headers={"User-Agent": _UA}) as c:
            cur = url
            for _ in range(6):
                r = c.get(cur)
                nxt = r.headers.get("location") if 300 <= r.status_code < 400 else None
                if not nxt:
                    break
                cur = str(httpx.URL(cur).join(nxt))
                host = _host(cur)
                if not _is_short(host) and _store_of(host) not in (None, "Loja"):
                    break  # chegou numa loja: não precisamos abrir a página dela
            if cur != url:
                final = cur
    except (httpx.HTTPError, ValueError):
        pass
    if len(_resolved) > 3000:
        _resolved.clear()
    _resolved[url] = final
    return final


def _key(url: str) -> str:
    host = _host(url)
    if m := affiliate._ASIN.search(url):
        return f"amz:{m.group(1).upper()}"
    if ids := mercadolivre.parse_ml_id(url):
        return f"ml:{ids[1]}"
    return f"u:{host}{urlsplit(url).path.rstrip('/').lower()}"[:160]


def _name(text: str) -> str | None:
    for raw in text.splitlines():
        line = _COUPON_LINE.sub("", _PRICE_TOKEN.sub("", _URL.sub("", raw)))
        line = re.sub(r"[^\wÀ-ÿ%&+\-.,/()\" ]+", " ", line)
        line = " ".join(line.split()).strip(" -–:|.,")
        line = _TRAIL.sub("", line).strip(" -–:|.,")
        if len(line) < 10 or sum(c.isalpha() for c in line) < 8:
            continue
        if _GENERIC.match(line) and len(line.split()) <= 4:
            continue
        return scrub(line)[:100] or None
    return None


def _coupon(text: str) -> str | None:
    for m in _COUPON.finditer(text):
        code = m.group(1)
        if any(c.isalpha() for c in code) and code.upper() not in _COUPON_STOP:
            return code
    return None


def _title_key(title: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", normalize(title))[:6])[:100]


@dataclass
class Parsed:
    title: str
    price: int | None
    coupon: str | None
    store: str
    url: str
    key: str


def _parse(text: str) -> tuple[Parsed | None, str, str | None]:
    """(oferta, motivo, domínio desconhecido). O motivo explica por que uma mensagem não virou oferta."""
    urls = _urls(text)
    if len({_host(u) for u in urls}) >= 3:  # listas de vários produtos/cupons: não cabem num cartão
        return None, "lista de vários links", None
    url = first_store_url(text)
    if not url:
        others = [_host(u) for u in urls if not any(_host(u) == d or _host(u).endswith("." + d) for d in _SOCIAL)]
        return None, ("loja não reconhecida" if others else "sem link de loja"), (others[0] if others else None)
    price, coupon = price_in_text(text), _coupon(text)
    if not (price or coupon):
        return None, "sem preço nem cupom", None
    store, target = _store_of(_host(url)) or "Loja", url
    if _is_short(_host(url)) and (final := resolve(url)):
        target = final
        real = _store_of(_host(final))
        if real and real != "Loja":
            store = real
    title = _name(text) or f"Oferta {store}"
    return Parsed(title, price, coupon, store, url, _key(target)), "virou oferta", None


def parse(text: str) -> Parsed | None:
    """Dados da oferta numa mensagem, ou None se não parece uma oferta com link de loja."""
    return _parse(text)[0]


# Diagnóstico (só contagens em memória, sem texto de mensagens): por que as mensagens são descartadas
_seen: deque = deque(maxlen=2000)


def stats(hours: int = 24) -> dict:
    since = utcnow() - timedelta(hours=hours)
    rows = [r for r in _seen if r[0] >= since]
    return {"total": len(rows), "reasons": Counter(r[1] for r in rows).most_common(),
            "unknown": Counter(r[2] for r in rows if r[2]).most_common(5)}


@dataclass
class Result:
    id: int
    needs_image: bool


def remember(source_title: str, text: str) -> Result | None:
    p, reason, unknown = _parse(text)
    _seen.append((utcnow(), reason, unknown))
    if p is None:
        return None
    tkey, now = _title_key(p.title), utcnow()
    with SessionLocal() as db:
        match = or_(Offer.key == p.key, (Offer.title_key == tkey) & (Offer.price_cents == p.price)) if tkey \
            else Offer.key == p.key
        o = db.scalar(select(Offer).where(match, Offer.updated_at >= now - MERGE_WINDOW)
                      .order_by(Offer.updated_at.desc()))
        if o is None:
            o = Offer(key=p.key, title_key=tkey, title=p.title, search=normalize(p.title)[:200], price_cents=p.price, coupon=p.coupon,
                      store=p.store, url=p.url, source_title=source_title[:255])
            db.add(o)
        else:
            if p.price and (o.price_cents is None or p.price < o.price_cents):
                o.old_price_cents, o.price_cents = o.price_cents, p.price
            o.coupon = o.coupon or p.coupon
            o.seen_count += 1
            o.updated_at = now
        db.commit()
        return Result(o.id, not o.image)


# ---------- imagem ----------

def image_path(offer_id: int):
    return get_settings().DATA_DIR / "offers" / f"{int(offer_id)}.jpg"


def save_image(offer_id: int, data: bytes) -> None:
    if not data or len(data) > 1_500_000:
        return
    path = image_path(offer_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    with SessionLocal() as db:
        if o := db.get(Offer, offer_id):
            o.image = True
            db.commit()


def pick_size(photo):
    """Menor miniatura com pelo menos 480 px (ou a maior): leve para o celular."""
    sizes = [s for s in getattr(photo, "sizes", []) if hasattr(s, "w") and hasattr(s, "h")]
    if not sizes:
        return None
    good = [s for s in sizes if max(s.w, s.h) >= 480]
    return min(good, key=lambda s: s.w * s.h) if good else max(sizes, key=lambda s: s.w * s.h)


# ---------- consulta e limpeza ----------

def stores_in_feed(db) -> list[str]:
    since = utcnow() - RETENTION
    return list(db.scalars(select(Offer.store).where(Offer.updated_at >= since).distinct().order_by(Offer.store)))


def feed(db, q: str = "", store: str = "", limit: int = 60, offset: int = 0) -> list[Offer]:
    stmt = select(Offer).where(Offer.updated_at >= utcnow() - RETENTION)
    if store:
        stmt = stmt.where(Offer.store == store)
    for word in normalize(q).split():
        stmt = stmt.where(Offer.search.contains(word))
    return list(db.scalars(stmt.order_by(Offer.updated_at.desc()).limit(limit).offset(offset)))


def cleanup() -> None:
    with SessionLocal() as db:
        old = db.scalars(select(Offer).where(Offer.updated_at < utcnow() - RETENTION)).all()
        for o in old:
            image_path(o.id).unlink(missing_ok=True)
        if old:
            db.execute(delete(Offer).where(Offer.id.in_([o.id for o in old])))
            db.commit()
