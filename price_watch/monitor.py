"""Consulta periódica dos preços.

Lojas "vm" são lidas aqui mesmo (scheduler, a cada minuto). Lojas "home" ficam na
fila do coletor de casa, que busca as páginas pelo IP residencial e devolve o HTML
para ser processado por process_page()."""

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select

from . import affiliate, fetcher, linking, mercadolivre, notifier
from .db import SessionLocal
from .models import Alert, PricePoint, Product, utcnow
from .pricing import extract, format_brl, host_of, should_notify

log = logging.getLogger(__name__)

BATCH = 20          # produtos por rodada
DOMAIN_GAP = 5.0    # segundos entre acessos à mesma loja (educação com o site)
HISTORY_EVERY = timedelta(hours=12)  # grava ponto repetido no máximo a cada 12 h
LEASE = timedelta(minutes=10)        # tempo que um produto fica reservado ao coletor
COLLECTOR_SILENT = timedelta(minutes=60)  # sem contato por mais que isso = aviso no bot

_lock = asyncio.Lock()  # uma consulta por vez (o Playwright pesa na VM pequena)
_last_hit: dict[str, float] = {}

# Estado do coletor de casa (em memória; zera ao reiniciar)
collector = {"last_seen": None, "warned": False}
_leases: dict[int, datetime] = {}


def _aware(dt):
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt


def due_product_ids(via: str = "vm", limit: int = BATCH) -> list[int]:
    now = utcnow()
    with SessionLocal() as db:
        items = db.scalars(select(Product).where(Product.active)).all()
        due = [p for p in items
               if p.store.fetch_via == via
               and (p.last_checked_at is None
                    or _aware(p.last_checked_at) + timedelta(minutes=p.store.interval_minutes) <= now)]
        due.sort(key=lambda p: _aware(p.last_checked_at) or now - timedelta(days=3650))
        return [p.id for p in due[:limit]]


async def check_due() -> None:
    for via in ("vm", "api"):
        for pid in await asyncio.to_thread(due_product_ids, via):
            await check_product(pid)


async def fix_short_link(product_id: int) -> None:
    """Produto cadastrado com link curto: troca pelo endereço real e pela loja certa."""
    with SessionLocal() as db:
        p = db.get(Product, product_id)
        if p is None or not linking.needs_resolving(p.url):
            return
        short = p.url
    final = await linking.resolve(short)
    if final == short:
        return
    with SessionLocal() as db:
        p = db.get(Product, product_id)
        if db.scalar(select(Product).where(Product.url == final, Product.id != product_id)):
            p.last_error = "produto repetido: ele já está na sua lista (pode remover este)"
            db.commit()
            return
        old_store = p.store
        p.source_url = p.source_url or short
        p.url = final
        p.store = linking.store_for(db, final)
        p.last_error = None
        db.flush()
        if linking.needs_resolving("https://" + old_store.domain) and not old_store.products:
            db.delete(old_store)  # loja criada por engano para o encurtador
        db.commit()


async def check_product(product_id: int) -> None:
    """Baixa a página pelo próprio servidor e processa."""
    await fix_short_link(product_id)
    with SessionLocal() as db:
        p = db.get(Product, product_id)
        if p is None:
            return
        url, mode, via, source = p.url, p.store.read_mode, p.store.fetch_via, p.source_url
    if via == "home":  # virou loja do coletor depois de seguir o link curto
        request_now(product_id)
        return

    if via == "api":  # API oficial (Mercado Livre): dados prontos, sem baixar página
        try:
            ex, error = await mercadolivre.fetch_price(url, source), None
            error = None if ex.price else "a API não informou preço para este anúncio"
        except fetcher.FetchError as e:
            ex, error = None, str(e)
        except Exception as e:  # noqa: BLE001
            ex, error = None, f"erro inesperado na API ({type(e).__name__})"
        await _finish(product_id, ex, error)
        return

    async with _lock:
        host = host_of(url)
        wait = DOMAIN_GAP - (time.monotonic() - _last_hit.get(host, 0))
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            html, error = await fetcher.fetch(url, mode), None
        except fetcher.FetchError as e:
            html, error = None, str(e)
        except Exception as e:  # noqa: BLE001 — uma loja com problema não pode parar as outras
            html, error = None, f"erro inesperado na leitura ({type(e).__name__})"
        finally:
            _last_hit[host] = time.monotonic()
    await process_page(product_id, html, error)


# ---------- coletor de casa ----------

def collector_jobs(limit: int = 5) -> list[dict]:
    """Produtos de lojas "home" na hora de consultar, reservados ao coletor."""
    now = utcnow()
    collector["last_seen"] = now
    collector["warned"] = False
    for pid in [k for k, until in _leases.items() if until <= now]:
        del _leases[pid]
    ids = [pid for pid in due_product_ids("home", limit=50) if pid not in _leases][:limit]
    with SessionLocal() as db:
        jobs = []
        for pid in ids:
            p = db.get(Product, pid)
            _leases[pid] = now + LEASE
            jobs.append({"id": p.id, "url": p.url})
        return jobs


async def process_collected(product_id: int, status: int, html: str | None, error: str | None) -> None:
    _leases.pop(product_id, None)
    if html is not None and error is None:
        try:
            fetcher.check_response(status, html)
        except fetcher.FetchError as e:
            html, error = None, str(e)
    await process_page(product_id, html, error)


def request_now(product_id: int) -> None:
    """"Consultar" de um produto lido pelo coletor: entra na fila já."""
    _leases.pop(product_id, None)
    with SessionLocal() as db:
        if p := db.get(Product, product_id):
            p.last_checked_at = None
            db.commit()


def collector_silent_for() -> timedelta | None:
    seen = collector["last_seen"]
    return None if seen is None else utcnow() - seen


async def collector_watchdog(started_at: datetime) -> None:
    """Avisa uma vez pelo bot se o coletor sumir e houver produtos esperando por ele."""
    if collector["warned"] or not await asyncio.to_thread(_has_home_products):
        return
    silent = collector_silent_for() or (utcnow() - started_at)
    if silent >= COLLECTOR_SILENT:
        collector["warned"] = True
        mins = int(silent.total_seconds() // 60)
        await notifier.send(f"⚠️ <b>Coletor de casa sem contato há {mins} min.</b>\n"
                            "Os preços das lojas lidas pelo celular estão parados. "
                            "Confira se o Termux continua aberto no celular.")


def _has_home_products() -> bool:
    with SessionLocal() as db:
        return any(p.store.fetch_via == "home" for p in db.scalars(select(Product).where(Product.active)))


# ---------- processamento comum ----------

async def process_page(product_id: int, html: str | None, error: str | None) -> None:
    ex = None
    if html is not None and error is None:
        with SessionLocal() as db:
            p = db.get(Product, product_id)
            if p is None:
                return
            url, selector = p.url, p.store.price_selector
        try:
            ex = extract(html, url, selector)
            error = None if ex.price else "preço não encontrado na página"
        except Exception as e:  # noqa: BLE001
            ex, error = None, f"erro ao interpretar a página ({type(e).__name__})"
    await _finish(product_id, ex, error)


async def _finish(product_id: int, ex, error: str | None) -> None:
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
                "target": p.target_cents, "reason": reason, "url": affiliate.buy_url(p.url)}


def _save_alert(a: dict, ok: bool) -> None:
    change = f"{format_brl(a['old'])} → {format_brl(a['new'])}" if a["old"] else format_brl(a["new"])
    with SessionLocal() as db:
        db.add(Alert(source="price", group_title=a["store"], keywords=a["reason"],
                     snippet=f"{a['name']}\n{change}", link=a["url"], delivered=ok))
        db.commit()
