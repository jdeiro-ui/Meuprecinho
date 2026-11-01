"""Diagnóstico da API do Mercado Livre: mostra quais endpoints a sua conexão acessa.

Uso: .venv/bin/python -m price_watch.ml_diag [MLB123...]
Mostra só o status e o motivo de cada chamada; nunca imprime tokens."""

import asyncio
import sys

from . import mercadolivre
from .db import init_db


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
        for path in paths:
            r = await c.get(path, headers={"Authorization": f"Bearer {token}"})
            why = mercadolivre._why(r) if r.status_code != 200 else "ok"
            print(f"{r.status_code}  {path.split('?')[0]:42} {why}")


if __name__ == "__main__":
    asyncio.run(main(sys.argv[1] if len(sys.argv) > 1 else "MLB4232914111"))
