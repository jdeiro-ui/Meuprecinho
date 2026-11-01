"""Diagnóstico da API do Mercado Livre: mostra quais endpoints a sua conexão acessa.

Uso: .venv/bin/python -m price_watch.ml_diag [MLB123...]
Mostra só o status e o motivo de cada chamada; nunca imprime tokens."""

import asyncio
import sys

import httpx

from . import linking, mercadolivre
from .db import init_db
from .fetcher import HEADERS


AFFILIATE = "https://meli.la/1KneFL7"


async def main(ml_id: str) -> None:
    init_db()
    st = mercadolivre.status()
    print(f"Conexão: {'sim' if st['connected'] else 'NÃO'} | renovação automática: "
          f"{'sim' if st['renewable'] else 'NÃO (falta offline_access)'}")
    token = await mercadolivre._access_token()
    paths = [
        "/users/me",
        f"/items/{ml_id}",
        f"/items?ids={ml_id}",
        f"/items/{ml_id}/sale_price?context=channel_marketplace",
        "/sites/MLB/search?q=camiseta&limit=1",
        "/products/search?status=active&site_id=MLB&q=camiseta&limit=1",
    ]
    async with mercadolivre._client() as c:
        catalog = None
        for path in paths:
            r = await c.get(path, headers={"Authorization": f"Bearer {token}"})
            why = mercadolivre._why(r) if r.status_code != 200 else "ok"
            if r.status_code == 200 and path.startswith("/items?ids="):
                entry = (r.json() or [{}])[0]
                body = entry.get("body") or {}
                why = f"anúncio: código {entry.get('code')}"
                if entry.get("code") == 200:
                    catalog = body.get("catalog_product_id")
                    why += f" | preço {body.get('price')} | catálogo {catalog}"
                else:
                    why += f" | {body.get('error') or body.get('message') or ''}"
            print(f"{r.status_code}  {path.split('?')[0]:42} {why}")
        if not catalog:  # pega um produto qualquer do catálogo para testar
            r = await c.get("/products/search?status=active&site_id=MLB&q=camiseta&limit=1",
                            headers={"Authorization": f"Bearer {token}"})
            results = r.json().get("results") if r.status_code == 200 else None
            catalog = results[0]["id"] if results else None
        if catalog:
            for path in (f"/products/{catalog}", f"/products/{catalog}/items"):
                r = await c.get(path, headers={"Authorization": f"Bearer {token}"})
                why = mercadolivre._why(r) if r.status_code != 200 else "ok"
                if r.status_code == 200 and path.endswith("/items"):
                    why = f"{len(r.json().get('results') or [])} anúncios, preços " + \
                          str([i.get("price") for i in (r.json().get("results") or [])[:3]])
                elif r.status_code == 200:
                    why = f"buy box: {(r.json().get('buy_box_winner') or {}).get('price')}"
                print(f"{r.status_code}  {path:42} {why}")

    print("\nPágina do link de afiliado (meli.la):")
    try:
        async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=25) as c:
            r = await c.get(AFFILIATE)
        offer = linking.ml_social_offer(r.text) if r.status_code == 200 else None
        print(f"{r.status_code}  {'social' if linking.is_ml_social(str(r.url)) else str(r.url)[:60]}  "
              f"{offer if offer else 'sem dados do produto (bloqueado?)'}")
    except httpx.HTTPError as e:
        print("falhou:", type(e).__name__)


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "MLB4232914111"))
