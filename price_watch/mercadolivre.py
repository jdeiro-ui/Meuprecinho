"""Integração com a API oficial do Mercado Livre (OAuth 2.0 + itens/produtos).

As credenciais do app (ML_CLIENT_ID/ML_CLIENT_SECRET) vêm do .env; os tokens da
conta conectada ficam na tabela settings e são renovados sozinhos."""

import base64
import hashlib
import re
import secrets
import time
from urllib.parse import urlencode

import httpx

from .config import get_settings
from .db import SessionLocal
from .fetcher import FetchError
from .models import Setting
from .pricing import Extracted, parse_brl

AUTH_URL = "https://auth.mercadolivre.com.br/authorization"
API = "https://api.mercadolibre.com"
_KEYS = ("ml_access_token", "ml_refresh_token", "ml_expires_at", "ml_nickname")

# Para testes: permite trocar o transporte HTTP por um falso
transport: httpx.AsyncBaseTransport | None = None


def configured() -> bool:
    s = get_settings()
    return bool(s.ML_CLIENT_ID and s.ML_CLIENT_SECRET)


# ---------- identificação do produto pelo link ----------

_ITEM = re.compile(r"\bMLB-?(\d{6,})", re.I)
_PRODUCT = re.compile(r"/p/MLB(\d{6,})", re.I)
_ITEM_PARAM = re.compile(r"(?:item_id[:=]|wid=)MLB-?(\d{6,})", re.I)


def parse_ml_id(url: str) -> tuple[str, str] | None:
    """('item', 'MLB123') para anúncios; ('product', 'MLB123') para páginas de catálogo."""
    if m := _ITEM_PARAM.search(url):  # catálogo com anúncio escolhido
        return "item", f"MLB{m.group(1)}"
    if m := _PRODUCT.search(url):
        return "product", f"MLB{m.group(1)}"
    if m := _ITEM.search(url):
        return "item", f"MLB{m.group(1)}"
    return None


# ---------- tokens ----------

def _get(keys) -> dict:
    with SessionLocal() as db:
        return {k: (row.value if (row := db.get(Setting, k)) else None) for k in keys}


def _put(values: dict) -> None:
    with SessionLocal() as db:
        for k, v in values.items():
            row = db.get(Setting, k)
            if v is None:
                if row:
                    db.delete(row)
            elif row:
                row.value = str(v)
            else:
                db.add(Setting(key=k, value=str(v)))
        db.commit()


def account() -> str | None:
    """Apelido da conta conectada, ou None."""
    data = _get(("ml_refresh_token", "ml_nickname"))
    return (data["ml_nickname"] or "conta conectada") if data["ml_refresh_token"] else None


def disconnect() -> None:
    _put({k: None for k in _KEYS})


def new_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(redirect_uri: str, state: str, challenge: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "response_type": "code", "client_id": get_settings().ML_CLIENT_ID, "redirect_uri": redirect_uri,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    })


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=API, timeout=20, transport=transport,
                             headers={"Accept": "application/json"})


async def _token_request(data: dict) -> None:
    s = get_settings()
    data |= {"client_id": s.ML_CLIENT_ID, "client_secret": s.ML_CLIENT_SECRET}
    async with _client() as c:
        r = await c.post("/oauth/token", data=data)
        if r.status_code != 200:
            # Só o código: a resposta não traz dados pessoais, mas evitamos ecoar segredos
            raise FetchError(f"o Mercado Livre recusou a autorização (HTTP {r.status_code})")
        tok = r.json()
        values = {"ml_access_token": tok["access_token"],
                  "ml_expires_at": int(time.time()) + int(tok.get("expires_in", 21600)) - 120}
        if tok.get("refresh_token"):  # o refresh token muda a cada uso
            values["ml_refresh_token"] = tok["refresh_token"]
        _put(values)
        me = await c.get("/users/me", headers={"Authorization": f"Bearer {tok['access_token']}"})
        if me.status_code == 200:
            _put({"ml_nickname": me.json().get("nickname")})


async def exchange_code(code: str, redirect_uri: str, verifier: str) -> None:
    await _token_request({"grant_type": "authorization_code", "code": code,
                          "redirect_uri": redirect_uri, "code_verifier": verifier})


async def _access_token() -> str:
    if not configured():
        raise FetchError("API do Mercado Livre não configurada (ML_CLIENT_ID no .env)")
    data = _get(("ml_access_token", "ml_refresh_token", "ml_expires_at"))
    if not data["ml_refresh_token"]:
        raise FetchError("conecte sua conta do Mercado Livre na aba Conta")
    if not data["ml_access_token"] or int(data["ml_expires_at"] or 0) <= time.time():
        try:
            await _token_request({"grant_type": "refresh_token", "refresh_token": data["ml_refresh_token"]})
        except FetchError:
            raise FetchError("a conexão com o Mercado Livre expirou; conecte de novo na aba Conta")
        data = _get(("ml_access_token",))
    return data["ml_access_token"]


# ---------- preço ----------

async def _api_get(c: httpx.AsyncClient, path: str, token: str) -> dict:
    try:
        r = await c.get(path, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as e:
        raise FetchError(f"sem resposta da API do Mercado Livre ({type(e).__name__})") from e
    if r.status_code == 404:
        raise FetchError("anúncio não encontrado no Mercado Livre")
    if r.status_code in (401, 403):
        raise FetchError(f"a API do Mercado Livre recusou o acesso (HTTP {r.status_code})")
    if r.status_code != 200:
        raise FetchError(f"a API do Mercado Livre respondeu HTTP {r.status_code}")
    return r.json()


def _picture(d: dict) -> str | None:
    pics = d.get("pictures") or []
    if pics:
        return pics[0].get("secure_url") or pics[0].get("url")
    return d.get("secure_thumbnail") or d.get("thumbnail")


async def fetch_price(url: str) -> Extracted:
    ids = parse_ml_id(url)
    if ids is None:
        raise FetchError("não reconheci o código MLB no link do Mercado Livre")
    kind, ml_id = ids
    token = await _access_token()
    async with _client() as c:
        if kind == "item":
            item = await _api_get(c, f"/items/{ml_id}", token)
            return Extracted(
                price=parse_brl(item.get("price")),
                name=item.get("title"),
                image=_picture(item),
                available=item.get("status") == "active" and (item.get("available_quantity") or 0) > 0,
            )
        prod = await _api_get(c, f"/products/{ml_id}", token)
        winner = prod.get("buy_box_winner") or {}
        price = parse_brl(winner.get("price"))
        if price is None:  # sem vencedor da "buy box": pega o menor preço entre os anúncios
            items = (await _api_get(c, f"/products/{ml_id}/items", token)).get("results") or []
            prices = [p for p in (parse_brl(i.get("price")) for i in items) if p]
            price = min(prices) if prices else None
        return Extracted(price=price, name=prod.get("name"), image=_picture(prod),
                         available=price is not None and prod.get("status", "active") == "active")
