"""Consulta periódica dos preços (chamada pelo scheduler a cada minuto)."""

import asyncio
import logging
import time
from datetime import UTC, timedelta

from sqlalchemy import select

from . import fetcher, notifier
from .db import SessionLocal
from .models import Alert, PricePoint, Product, utcnow
from .pricing import extract, host_of, should_notify

log = logging.getLogger(__name__)

BATCH = 20          # produtos por rodada
DOMAIN_GAP = 5.0    # segundos entre acessos à mesma loja (educação com o site)
HISTORY_EVERY = timedelta(hours=12)  # grava ponto repetido no máximo a cada 12 h

_lock = asyncio.Lock()  # uma consulta por vez (o Playwright pesa na VM pequena)
_last_hit: dict[str, float] = {}


def _aware(dt):
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


def due_product_ids() -> list[int]:
    now = utcnow()
    with SessionLocal() as db:
        items = db.scalars(select(Product).where(Product.active)).all()
        due = [p for p in items
               if p.last_checked_at is None
               or _aware(p.last_checked_at) + timedelta(minutes=p.store.interval_minutes) <= now]
        due.sort(key=lambda p: _aware(p.last_checked_at) or now - timedelta(days=3650))
        return [p.id for p in due[:BATCH]]


async def check_due() -> None:
    for pid in await asyncio.to_thread(due_product_ids):
        await check_product(pid)


async def check_product(product_id: int) -> None:
    with SessionLocal() as db:
        p = db.get(Product, product_id)
        if p is None:
            return
        url, mode, selector = p.url, p.store.read_mode, p.store.price_selector

    async with _lock:
        host = host_of(url)
        wait = DOMAIN_GAP - (time.monotonic() - _last_hit.get(host, 0))
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            html = await fetcher.fetch(url, mode)
            ex = extract(html, url, selector)
            error = None if ex.price else "preço não encontrado na página"
        except fetcher.FetchError as e:
            ex, error = None, str(e)
        except Exception as e:  # noqa: BLE001 — uma loja com problema não pode parar as outras
            ex, error = None, f"erro inesperado na leitura ({type(e).__name__})"
        finally:
            _last_hit[host] = time.monotonic()

    alert = await asyncio.to_thread(_save_result, product_id, ex, error)
    if alert:
        ok = await notifier.send(notifier.format_price_alert(**alert))
        await asyncio.to_thread(_save_alert, alert, ok)
    if error:
        log.info("Produto %s: leitura falhou", product_id)  # sem URL nem conteúdo


def _save_result(product_id: int, ex, error: str | None) -> dict | None:
    """Grava a leitura e devolve os dados do aviso, se houver."""
    with SessionLocal() as db:
        p = db.get(Product, product_id)
        if p is None:
            return None
        p.last_checked_at = utcnow()
        p.last_error = error
        if error:
            db.commit()
            return None
        if ex.name and not p.name:
            p.name = ex.name
        if ex.image and not p.image_url:
            p.image_url = ex.image[:2000]
        p.available = ex.available
        old, lowest_before = p.current_cents, p.lowest_cents
        reason = should_notify(ex.price, ex.available, p.target_cents, lowest_before, p.last_notified_cents)

        last_point = db.scalar(select(PricePoint).where(PricePoint.product_id == p.id)
                               .order_by(PricePoint.checked_at.desc()).limit(1))
        if (last_point is None or last_point.cents != ex.price
                or _aware(last_point.checked_at) + HISTORY_EVERY <= utcnow()):
            db.add(PricePoint(product_id=p.id, cents=ex.price))

        p.current_cents = ex.price
        p.lowest_cents = min(x for x in (lowest_before, ex.price) if x is not None)
        if reason:
            p.last_notified_cents = ex.price
        elif p.target_cents is not None and ex.price > p.target_cents:
            p.last_notified_cents = None  # voltou acima do alvo: próxima queda avisa de novo
        db.commit()
        if not reason:
            return None
        return {"store": p.store.name, "name": p.name or "Produto", "old": old, "new": ex.price,
                "target": p.target_cents, "reason": reason, "url": p.url}


def _save_alert(a: dict, ok: bool) -> None:
    from .pricing import format_brl

    change = f"{format_brl(a['old'])} → {format_brl(a['new'])}" if a["old"] else format_brl(a["new"])
    with SessionLocal() as db:
        db.add(Alert(source="price", group_title=a["store"], keywords=a["reason"],
                     snippet=f"{a['name']}\n{change}", link=a["url"], delivered=ok))
        db.commit()
