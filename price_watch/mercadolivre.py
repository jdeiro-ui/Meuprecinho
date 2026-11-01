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
    return bool(s.ML_CLIENT_ID.strip() and s.ML_CLIENT_SECRET.strip())


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


def status() -> dict:
    """Situação da conexão: conectado, apelido, renovação automática e validade."""
    d = _get(_KEYS)
    expires = int(d["ml_expires_at"] or 0)
    renewable = bool(d["ml_refresh_token"])
    valid = bool(d["ml_access_token"]) and expires > time.time()
    return {"connected": renewable or valid, "nickname": d["ml_nickname"] or "conta conectada",
            "renewable": renewable, "expires_at": expires, "expired": not renewable and not valid
            and bool(d["ml_access_token"])}


def account() -> str | None:
    """Apelido da conta conectada, ou None."""
    st = status()
    return st["nickname"] if st["connected"] else None


def disconnect() -> None:
    _put({k: None for k in _KEYS})


def new_pkce() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def authorize_url(redirect_uri: str, state: str, challenge: str) -> str:
    return AUTH_URL + "?" + urlencode({
        "response_type": "code", "client_id": get_settings().ML_CLIENT_ID.strip(), "redirect_uri": redirect_uri,
        "state": state, "code_challenge": challenge, "code_challenge_method": "S256",
    })


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=API, timeout=20, transport=transport,
                             headers={"Accept": "application/json"})


class AuthError(FetchError):
    def __init__(self, status: int, code: str, detail: str):
        self.status, self.code = status, code
        why = f"{code}: {detail}" if detail else code
        super().__init__(f"o Mercado Livre recusou a autorização (HTTP {status}, {why})")


def _auth_error(r: httpx.Response) -> AuthError:
    try:
        body = r.json()
    except ValueError:
        body = {}
    secret = get_settings().ML_CLIENT_SECRET
    # "error"/"message" explicam o motivo (ex.: invalid_grant); nunca ecoamos o segredo
    code = str(body.get("error") or "erro")[:40]
    detail = str(body.get("message") or body.get("error_description") or "")[:160]
    if secret:
        detail = detail.replace(secret, "***")
    return AuthError(r.status_code, code, detail)


async def _token_request(data: dict) -> None:
    s = get_settings()
    data = data | {"client_id": s.ML_CLIENT_ID.strip(), "client_secret": s.ML_CLIENT_SECRET.strip()}
    async with _client() as c:
        r = await c.post("/oauth/token", data=data)
        if r.status_code != 200:
            raise _auth_error(r)
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
    data = {"grant_type": "authorization_code", "code": code, "redirect_uri": redirect_uri}
    try:
        await _token_request(data | {"code_verifier": verifier})
    except AuthError as e:
        if e.status != 400:
            raise
        # App sem PKCE ativado pode recusar o code_verifier: tenta sem ele
        await _token_request(data)


async def _access_token() -> str:
    if not configured():
        raise FetchError("API do Mercado Livre não configurada (ML_CLIENT_ID no .env)")
    data = _get(("ml_access_token", "ml_refresh_token", "ml_expires_at"))
    valid = bool(data["ml_access_token"]) and int(data["ml_expires_at"] or 0) > time.time()
    if valid:
        return data["ml_access_token"]
    if not data["ml_refresh_token"]:
        if data["ml_access_token"]:  # conectado sem offline_access: vale só ~6 h
            raise FetchError("a conexão com o Mercado Livre expirou; ative offline_access no app "
                             "do Mercado Livre e conecte de novo na aba Conta")
        raise FetchError("conecte sua conta do Mercado Livre na aba Conta")
    try:
        await _token_request({"grant_type": "refresh_token", "refresh_token": data["ml_refresh_token"]})
    except FetchError:
        raise FetchError("a conexão com o Mercado Livre expirou; conecte de novo na aba Conta")
    return _get(("ml_access_token",))["ml_access_token"]


# ---------- preço ----------

class ApiError(FetchError):
    def __init__(self, status: int, msg: str):
        self.status = status
        super().__init__(msg)


def _why(r: httpx.Response) -> str:
    """Motivo curto devolvido pelo Mercado Livre (ex.: blocked_by=PolicyAgent)."""
    try:
        b = r.json()
    except ValueError:
        return ""
    if not isinstance(b, dict):
        return ""
    parts = [str(b[k]) for k in ("code", "error", "blocked_by") if b.get(k)]
    if b.get("message"):
        parts.append(str(b["message"])[:120])
    return "; ".join(dict.fromkeys(parts))[:200]


async def _api_get(c: httpx.AsyncClient, path: str, token: str):
    try:
        r = await c.get(path, headers={"Authorization": f"Bearer {token}"})
    except httpx.HTTPError as e:
        raise FetchError(f"sem resposta da API do Mercado Livre ({type(e).__name__})") from e
    if r.status_code == 200:
        return r.json()
    why = _why(r)
    suffix = f": {why}" if why else ""
    if r.status_code == 404:
        raise ApiError(404, "anúncio não encontrado no Mercado Livre")
    if r.status_code in (401, 403):
        raise ApiError(r.status_code, f"a API do Mercado Livre recusou o acesso (HTTP {r.status_code}{suffix})")
    raise ApiError(r.status_code, f"a API do Mercado Livre respondeu HTTP {r.status_code}{suffix}")


async def _item(c: httpx.AsyncClient, ml_id: str, token: str) -> Extracted:
    """Anúncio: tenta /items; se recusado, a consulta em lote e, por fim, só o preço."""
    def from_item(item: dict) -> Extracted:
        return Extracted(
            price=parse_brl(item.get("price")),
            name=item.get("title"),
            image=_picture(item),
            available=item.get("status") == "active" and (item.get("available_quantity") or 0) > 0,
        )

    try:
        return from_item(await _api_get(c, f"/items/{ml_id}", token))
    except ApiError as first:
        if first.status not in (401, 403):
            raise
        try:
            batch = await _api_get(c, f"/items?ids={ml_id}", token)
            entry = batch[0] if isinstance(batch, list) and batch else {}
            if entry.get("code") == 200 and isinstance(entry.get("body"), dict):
                return from_item(entry["body"])
        except ApiError:
            pass
        try:
            sale = await _api_get(c, f"/items/{ml_id}/sale_price?context=channel_marketplace", token)
            price = parse_brl(sale.get("amount"))
            if price:
                return Extracted(price=price, available=True)
        except ApiError:
            pass
        raise first


def _picture(d: dict) -> str | None:
    pics = d.get("pictures") or []
    if pics:
        return pics[0].get("secure_url") or pics[0].get("url")
    return d.get("secure_thumbnail") or d.get("thumbnail")


BLOCKED_HINT = ("o Mercado Livre não libera a leitura deste anúncio pela API; cadastre o "
                "produto pelo seu link de afiliado (meli.la)")


async def fetch_price(url: str, source_url: str | None = None) -> Extracted:
    from . import linking

    ids = parse_ml_id(url)
    # 1) Link de afiliado: a página que ele abre traz preço, nome e foto do produto
    if source_url and linking.needs_resolving(source_url):
        offer = await linking.ml_offer_from_link(source_url)
        if offer and offer["price"] and (ids is None or offer["id"] == ids[1]):
            return Extracted(price=offer["price"], name=offer["name"], image=offer["image"], available=True)
    # 2) API oficial (hoje só libera catálogo e anúncios da própria conta)
    if ids is None:
        raise FetchError("não reconheci o código MLB no link do Mercado Livre")
    kind, ml_id = ids
    token = await _access_token()
    async with _client() as c:
        if kind == "item":
            try:
                return await _item(c, ml_id, token)
            except ApiError as e:
                if e.status == 403 and "access_denied" in str(e):
                    raise FetchError(BLOCKED_HINT) from e
                raise
        prod = await _api_get(c, f"/products/{ml_id}", token)
        winner = prod.get("buy_box_winner") or {}
        price = parse_brl(winner.get("price"))
        if price is None:  # sem vencedor da "buy box": pega o menor preço entre os anúncios
            items = (await _api_get(c, f"/products/{ml_id}/items", token)).get("results") or []
            prices = [p for p in (parse_brl(i.get("price")) for i in items) if p]
            price = min(prices) if prices else None
        return Extracted(price=price, name=prod.get("name"), image=_picture(prod),
                         available=price is not None and prod.get("status", "active") == "active")
