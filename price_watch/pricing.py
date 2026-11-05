"""Leitura de preços: funções puras (sem rede nem banco), fáceis de testar.

Preços circulam em centavos (int) para evitar erros de arredondamento."""

import json
import re
from dataclasses import dataclass
from urllib.parse import urlsplit

from bs4 import BeautifulSoup


@dataclass
class Extracted:
    price: int | None = None  # centavos
    name: str | None = None
    image: str | None = None
    available: bool | None = None
    gtin: str | None = None   # EAN/código de barras
    brand: str | None = None
    model: str | None = None


# ---------- números ----------

_NUM = re.compile(r"\d[\d.,]*")


def parse_brl(value) -> int | None:
    """'R$ 1.299,90' / '1299.90' / 1299.9 -> 129990 centavos."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        cents = round(float(value) * 100)
        return cents if cents > 0 else None
    m = _NUM.search(str(value).replace("\xa0", " "))
    if not m:
        return None
    s = m.group().rstrip(".,")
    if "." in s and "," in s:
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        s = s.replace("." if dec == "," else ",", "").replace(dec, ".")
    elif "," in s:
        whole, _, frac = s.rpartition(",")
        s = f"{whole.replace(',', '')}.{frac}" if len(frac) <= 2 else s.replace(",", "")
    elif "." in s:
        whole, _, frac = s.rpartition(".")
        # "1.299" e "12.990" são milhares no Brasil; "1299.9" é decimal
        if len(frac) == 3 or s.count(".") > 1:
            s = s.replace(".", "")
    try:
        cents = round(float(s) * 100)
    except ValueError:
        return None
    return cents if cents > 0 else None


def format_brl(cents: int | None) -> str:
    if cents is None:
        return "—"
    reais, cent = divmod(int(cents), 100)
    return f"R$ {reais:,}".replace(",", ".") + f",{cent:02d}"


# ---------- extração da página ----------

def _walk(node):
    if isinstance(node, dict):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def _is_product(d: dict) -> bool:
    t = d.get("@type")
    types = t if isinstance(t, list) else [t]
    return any(str(x).lower() in ("product", "productgroup") for x in types)


def _offer_prices(offers) -> tuple[list[int], bool | None]:
    prices: list[int] = []
    available = None
    for o in _walk(offers):
        for key in ("price", "lowPrice"):
            p = parse_brl(o.get(key))
            if p:
                prices.append(p)
        av = str(o.get("availability", "")).lower()
        if av:
            in_stock = "instock" in av or "limitedavailability" in av or "presale" in av
            available = in_stock or bool(available)
    return prices, available


def normalize_gtin(value) -> str | None:
    """EAN/GTIN só com dígitos, sem zeros à esquerda extras (EAN-13 como base)."""
    digits = re.sub(r"\D", "", str(value or ""))
    if not 8 <= len(digits) <= 14:
        return None
    return digits.lstrip("0").zfill(13) if len(digits) > 8 else digits


def _identifiers(d: dict) -> tuple[str | None, str | None, str | None]:
    gtin = None
    for key in ("gtin13", "gtin", "gtin12", "gtin14", "gtin8", "ean"):
        if gtin := normalize_gtin(d.get(key)):
            break
    brand = d.get("brand")
    if isinstance(brand, dict):
        brand = brand.get("name")
    if isinstance(brand, list):
        brand = brand[0] if brand and isinstance(brand[0], str) else None
    model = d.get("mpn") or d.get("model")
    if isinstance(model, dict):
        model = model.get("name")
    clean = lambda v: " ".join(str(v).split())[:120] if v and isinstance(v, (str, int)) else None  # noqa: E731
    return gtin, clean(brand), clean(model)


def _first_image(img) -> str | None:
    if isinstance(img, str):
        return img
    if isinstance(img, list) and img:
        return _first_image(img[0])
    if isinstance(img, dict):
        return img.get("url") or img.get("contentUrl")
    return None


def _from_jsonld(soup: BeautifulSoup) -> Extracted:
    out = Extracted()
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or tag.get_text() or "")
        except (ValueError, TypeError):
            continue
        for d in _walk(data):
            if not _is_product(d) or "offers" not in d:
                continue
            prices, available = _offer_prices(d["offers"])
            if prices:
                out.price = min(prices)
                out.name = out.name or d.get("name")
                out.image = out.image or _first_image(d.get("image"))
                out.available = available
                out.gtin, out.brand, out.model = _identifiers(d)
                return out
    return out


_META_PRICE = ("product:price:amount", "og:price:amount", "price", "twitter:data1")


def _from_meta(soup: BeautifulSoup) -> int | None:
    for key in _META_PRICE:
        for attr in ("property", "itemprop", "name"):
            tag = soup.find("meta", attrs={attr: key})
            if tag and (p := parse_brl(tag.get("content"))):
                return p
    tag = soup.find(attrs={"itemprop": "price"})
    if tag:
        return parse_brl(tag.get("content") or tag.get_text(" ", strip=True))
    return None


# Seletores específicos para lojas sem dados estruturados confiáveis
SITE_SELECTORS = {
    "amazon.com.br": [
        "#corePriceDisplay_desktop_feature_div .a-price .a-offscreen",
        "#corePrice_feature_div .a-price .a-offscreen",
        "#apex_desktop .a-price .a-offscreen",
        "#priceblock_dealprice",
        "#priceblock_ourprice",
        "#twister-plus-price-data-price",
    ],
    "mercadolivre.com.br": [".ui-pdp-price__second-line [itemprop=price]",
                            ".ui-pdp-price__second-line .andes-money-amount__fraction"],
}


def _by_selector(soup: BeautifulSoup, selector: str) -> int | None:
    try:
        tag = soup.select_one(selector)
    except Exception:  # noqa: BLE001 — seletor inválido digitado pelo usuário
        return None
    if tag is None:
        return None
    return parse_brl(tag.get("content") or tag.get("value") or tag.get_text(" ", strip=True))


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower().removeprefix("www.")


def _site_rules(host: str) -> list[str]:
    for domain, sels in SITE_SELECTORS.items():
        if host == domain or host.endswith("." + domain):
            return sels
    return []


def extract(html: str, url: str, selector: str | None = None) -> Extracted:
    """Preço, nome e imagem do produto. Ordem: seletor da loja (definido pelo
    usuário) > JSON-LD (schema.org) > metatags > seletores conhecidos."""
    soup = BeautifulSoup(html, "html.parser")
    out = _from_jsonld(soup)
    if selector and (p := _by_selector(soup, selector)):
        out.price = p
    if out.price is None:
        out.price = _from_meta(soup)
    if out.price is None:
        for sel in _site_rules(host_of(url)):
            if p := _by_selector(soup, sel):
                out.price = p
                break
    if not out.name:
        og = soup.find("meta", property="og:title")
        h1 = soup.find("h1")
        title = soup.find("title")
        out.name = (og and og.get("content")) or (h1 and h1.get_text(" ", strip=True)) \
            or (title and title.get_text(" ", strip=True)) or None
    if not out.image:
        og = soup.find("meta", property="og:image")
        out.image = og.get("content") if og else None
    if out.name:
        out.name = " ".join(str(out.name).split())
        # "Produto X | Nome da Loja" -> "Produto X"
        head, sep, tail = out.name.rpartition(" | ")
        if sep and head and len(tail) <= 40:
            out.name = head
        out.name = out.name[:300]
    return out


# ---------- decisão de aviso ----------

def should_notify(new: int | None, available: bool | None, target: int | None,
                  lowest_before: int | None, last_notified: int | None) -> str | None:
    """Motivo do aviso, ou None. Com alvo: avisa ao ficar abaixo dele e de novo
    se cair ainda mais. Sem alvo: avisa quando bate o menor preço já visto."""
    if new is None or available is False:
        return None
    if target is not None:
        if new <= target and (last_notified is None or new < last_notified):
            return "abaixo do seu preço-alvo"
        return None
    if lowest_before is not None and new < lowest_before:
        return "menor preço já visto"
    return None


# ---------- quantidade na embalagem ----------

_PACK_PATTERNS = [
    # caixa com 12 / cx 12 / display c/ 12 / kit 12 / pack de 6 / fardo com 24
    r"\b(?:caixa|cx|display|pack|kit|fardo|pacote|combo|cartela)\s*(?:c/|com|de|c\.)?\s*(\d{1,3})\b(?!\s*(?:g|gr|kg|ml|l|mg)\b)",
    # 12 unidades / 12 un / 12 und / 12 barras / 6 latas
    r"\b(\d{1,3})\s*(?:unidades|unidade|unid|und|un|barras|barrinhas|latas|garrafas|capsulas|saches|sachês|pacotes|tubos)\b",
    # c/12 / c/ 12
    r"\bc/\s*(\d{1,3})\b",
    # 12 x 30g / 12x40g (quantidade x peso; "12x de R$" de parcelamento não casa)
    r"\b(\d{1,3})\s*x\s*\d+(?:[.,]\d+)?\s*(?:g|gr|ml|kg|l)\b",
    # 30g x 12 / 30g c/12
    r"\d\s*(?:g|gr|ml)\s*x\s*(\d{1,3})\b",
]
_PACK_RE = [re.compile(p) for p in _PACK_PATTERNS]


def pack_qty(text: str | None) -> int:
    """Unidades na embalagem a partir do nome ou da mensagem ("Caixa com 12", "12 un",
    "c/12", "12x30g"). Sem indicação, 1."""
    from .rules import normalize

    t = normalize(text or "")
    for rx in _PACK_RE:
        if m := rx.search(t):
            n = int(m.group(1))
            if 2 <= n <= 200:
                return n
    return 1


# ---------- utilidades ----------

_MSG_PRICE = re.compile(r"R\$\s*(\d{1,3}(?:\.\d{3})+(?:,\d{1,2})?|\d+(?:,\d{1,2})?)", re.I)
# Valores que não são o preço do produto: parcela, cupom, desconto, cashback, frete
_NOT_PRICE_BEFORE = re.compile(
    r"(?:\d+\s*x\s*(?:de\s*)?|parcelas?\s*(?:de\s*)?|cupom\s*(?:de\s*)?|desconto\s*(?:de\s*)?|"
    r"economi[sz]e\s*|economia\s*(?:de\s*)?|cashback\s*(?:de\s*)?|frete\s*(?:de\s*)?|"
    r"off\s*(?:de\s*)?|ganhe\s*|volta\s*)$", re.I)
_NOT_PRICE_AFTER = re.compile(r"^\s*(?:off\b|de\s+desconto|de\s+cashback|de\s+volta|em\s+cupom|"
                              r"de\s+frete|na\s+primeira\s+compra)", re.I)


def price_in_text(text: str) -> int | None:
    """Preço da oferta numa mensagem: o menor "R$ ..." (promoções trazem "de R$ X por
    R$ Y"), ignorando parcelas ("10x de R$ 199"), cupons ("cupom de R$ 50", "R$ 50 OFF"),
    descontos, cashback e frete."""
    text = text or ""
    prices = []
    for m in _MSG_PRICE.finditer(text):
        before = text[max(0, m.start() - 25):m.start()].strip()
        if _NOT_PRICE_BEFORE.search(before) or _NOT_PRICE_AFTER.match(text[m.end():m.end() + 25]):
            continue
        if p := parse_brl(m.group(1)):
            prices.append(p)
    return min(prices) if prices else None


_URL = re.compile(r"https?://[^\s<>\"']+")


def find_url(text: str | None) -> str | None:
    """Primeiro link de um texto (apps de loja compartilham 'Olha isso! https://...')."""
    m = _URL.search(text or "")
    return m.group().rstrip(").,;!") if m else None


def match_store(host: str, domains: list[str]) -> str | None:
    """Domínio cadastrado que cobre o host (ex.: produto.mercadolivre.com.br)."""
    host = host.lower().removeprefix("www.")
    best = None
    for d in domains:
        if host == d or host.endswith("." + d):
            if best is None or len(d) > len(best):
                best = d
    return best
