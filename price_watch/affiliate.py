"""Links de compra com o código de afiliado do MeuPrecinho.

O app guarda o endereço limpo do produto e gera o link de compra na hora, com o
código de afiliado configurado no .env. Assim, qualquer produto cadastrado por
qualquer usuário leva ao link do app, e trocar o código vale para tudo de uma vez."""

import hashlib
import hmac
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from .config import get_settings
from .pricing import host_of

# Parâmetros de rastreio/afiliado de terceiros que removemos ao guardar o link
_STRIP = re.compile(
    r"^(utm_\w+|matt_\w+|tag|ref|ref_|linkcode|linkid|camp|creative|creativeasin|ascsubtag|"
    r"tracking_id|reco_\w+|c_id|c_uid|sid|source|forceinapp|fbclid|gclid|aff_\w+|affiliate\w*|"
    r"smid|psc|th|spla|pd_rd_\w+|pf_rd_\w+|content-id|keywords|qid|sr|sprefix|crid|dib\w*)$",
    re.I,
)
_ASIN = re.compile(r"/(?:dp|gp/product|gp/aw/d|exec/obidos/asin)/([A-Z0-9]{10})", re.I)


def _is(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def clean_url(url: str) -> str:
    """Endereço do produto sem códigos de afiliado/rastreio de terceiros."""
    host = host_of(url)
    if _is(host, "amazon.com.br") and (m := _ASIN.search(url)):
        return f"https://www.amazon.com.br/dp/{m.group(1).upper()}"
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True) if not _STRIP.match(k)]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def configured_for(url: str) -> bool:
    s = get_settings()
    host = host_of(url)
    if _is(host, "amazon.com.br"):
        return bool(s.AMAZON_TAG)
    if _is(host, "mercadolivre.com.br"):
        return bool(s.ML_MATT_WORD and s.ML_MATT_TOOL)
    return False


def buy_url(url: str) -> str:
    """Link de compra com o código de afiliado do app (ou o link limpo, se a loja
    ainda não tiver código configurado)."""
    s = get_settings()
    clean = clean_url(url)
    host = host_of(clean)
    if _is(host, "amazon.com.br") and s.AMAZON_TAG and _ASIN.search(clean):
        return f"{clean}?tag={s.AMAZON_TAG}"
    if _is(host, "mercadolivre.com.br") and s.ML_MATT_WORD and s.ML_MATT_TOOL:
        sep = "&" if urlsplit(clean).query else "?"
        return f"{clean}{sep}" + urlencode({"matt_word": s.ML_MATT_WORD, "matt_tool": s.ML_MATT_TOOL})
    return clean


def click_sig(product_id: int) -> str:
    """Assinatura curta do link /r/<id>: o botão funciona fora do painel (ex.: bot)
    sem permitir varrer os produtos de todo mundo."""
    key = get_settings().SECRET_KEY.encode()
    return hmac.new(key, f"r{product_id}".encode(), hashlib.sha256).hexdigest()[:12]
