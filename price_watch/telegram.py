"""Conta do usuário via Telethon: pareamento, lista de grupos e listener."""

import asyncio
import html
import logging
from dataclasses import dataclass
from datetime import timedelta

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from telethon import TelegramClient, events
from telethon.tl.types import MessageEntityTextUrl
from telethon.errors import (
    ApiIdInvalidError,
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)

from . import notifier, offers
from .config import get_settings
from .db import SessionLocal
from .models import Alert, Item, Rule, SeenMessage, TgGroup, User, utcnow
from .item_rules import accessory_conflict, variant_conflict, variant_signature
from .pricing import format_brl, pack_qty, price_in_text
from .rules import RuleSpec, match_message, message_link, snippet, split_terms

log = logging.getLogger(__name__)


class PairingError(Exception):
    pass


@dataclass(frozen=True)
class GroupInfo:
    title: str
    username: str | None


class TelegramManager:
    def __init__(self) -> None:
        self.client: TelegramClient | None = None
        self.me_name: str | None = None
        self._phone: str | None = None
        self._code_hash: str | None = None
        self.awaiting: str | None = None  # None | "code" | "password"
        # Cache em memória para o listener não consultar o banco a cada mensagem
        self.groups: dict[int, GroupInfo] = {}
        self.rules: list[RuleSpec] = []
        self.user_chats: dict[int, str | None] = {}
        self.admins: set[int] = set()
        self._lock = asyncio.Lock()
        self._connect_task: asyncio.Task | None = None

    # ---------- ciclo de vida ----------

    def _new_client(self) -> TelegramClient:
        s = get_settings()
        client = TelegramClient(
            s.telethon_session,
            s.API_ID,
            s.API_HASH,
            auto_reconnect=True,
            # Poucas tentativas por vez; o watchdog do scheduler reconecta depois
            connection_retries=5,
            retry_delay=5,
            device_model="MeuPrecinho",
        )
        client.add_event_handler(self._on_message, events.NewMessage(incoming=True))
        client.add_event_handler(self._on_edit, events.MessageEdited(incoming=True))
        return client

    async def start(self) -> None:
        await asyncio.to_thread(self.reload)
        if not get_settings().telegram_configured:
            log.info("API_ID/API_HASH ausentes; Telegram desativado")
            return
        self.client = self._new_client()
        # Em segundo plano para não travar a subida do app se a rede estiver fora
        self._connect_task = asyncio.create_task(self.ensure_connected())

    async def stop(self) -> None:
        if self.client:
            await self.client.disconnect()

    async def ensure_connected(self) -> None:
        """Chamado no início e periodicamente pelo scheduler (watchdog)."""
        if not self.client:
            return
        async with self._lock:
            try:
                if not self.client.is_connected():
                    await self.client.connect()
                    log.info("Telegram conectado")
                if self.me_name is None and await self.client.is_user_authorized():
                    me = await self.client.get_me()
                    self.me_name = me.first_name or "conta"
                    # garante que o Telethon receba atualizações desde já
                    await self.client.catch_up()
            except Exception as exc:  # noqa: BLE001 — o watchdog não pode morrer
                log.warning("Telegram indisponível (%s); nova tentativa em breve", type(exc).__name__)

    @property
    def connected(self) -> bool:
        return bool(self.client and self.client.is_connected())

    @property
    def authorized(self) -> bool:
        return self.me_name is not None

    # ---------- pareamento ----------

    async def send_code(self, phone: str) -> None:
        if not self.client:
            raise PairingError("Configure API_ID e API_HASH no .env")
        await self.ensure_connected()
        try:
            sent = await self.client.send_code_request(phone)
        except ApiIdInvalidError:
            raise PairingError("API_ID/API_HASH inválidos: confira em my.telegram.org, "
                               "corrija o .env e reinicie o serviço")
        except PhoneNumberInvalidError:
            raise PairingError("Número inválido. Use o formato +5511999999999")
        except FloodWaitError as e:
            raise PairingError(f"Muitas tentativas. Aguarde {e.seconds}s")
        self._phone, self._code_hash, self.awaiting = phone, sent.phone_code_hash, "code"

    async def sign_in_code(self, code: str) -> None:
        if not (self.client and self._phone):
            raise PairingError("Comece informando o telefone")
        try:
            await self.client.sign_in(self._phone, code.strip(), phone_code_hash=self._code_hash)
        except SessionPasswordNeededError:
            self.awaiting = "password"
            return
        except PhoneCodeInvalidError:
            raise PairingError("Código incorreto")
        except PhoneCodeExpiredError:
            self.awaiting = None
            raise PairingError("Código expirado; peça outro")
        await self._paired()

    async def sign_in_password(self, password: str) -> None:
        if not self.client or self.awaiting != "password":
            raise PairingError("Senha 2FA não solicitada")
        try:
            await self.client.sign_in(password=password)
        except PasswordHashInvalidError:
            raise PairingError("Senha 2FA incorreta")
        await self._paired()

    async def _paired(self) -> None:
        self._phone = self._code_hash = self.awaiting = None
        await self.ensure_connected()
        log.info("Conta do Telegram pareada")

    async def logout(self) -> None:
        if self.client:
            try:
                await self.client.log_out()  # também apaga o arquivo .session
            except Exception:  # noqa: BLE001 — sair mesmo se a rede falhar
                await self.client.disconnect()
        self.me_name = None
        self.awaiting = None
        self.client = self._new_client()
        await self.ensure_connected()

    # ---------- grupos ----------

    async def refresh_groups(self) -> int:
        """Sincroniza a tabela tg_groups com os diálogos da conta."""
        if not (self.client and self.authorized):
            raise PairingError("Conta do Telegram não pareada")
        found: dict[int, tuple[str, str | None, str]] = {}
        async for d in self.client.iter_dialogs():
            if not (d.is_group or d.is_channel):
                continue
            ent = d.entity
            if getattr(ent, "megagroup", False):
                kind = "supergroup"
            elif d.is_channel:
                kind = "channel"
            else:
                kind = "group"
            found[d.id] = (d.name or "(sem nome)", getattr(ent, "username", None), kind)
        await asyncio.to_thread(_sync_groups, found)
        await asyncio.to_thread(self.reload)
        return len(found)

    # ---------- listener ----------

    def reload(self) -> None:
        """Recarrega grupos monitorados e regras ativas do banco."""
        with SessionLocal() as db:
            groups = db.scalars(select(TgGroup).where(TgGroup.monitored)).all()
            rules = db.scalars(select(Rule).where(Rule.active)).all()
            self.groups = {g.chat_id: GroupInfo(g.title, g.username) for g in groups}
            # Conversa de cada usuário com o bot (para onde vão os avisos)
            users = db.scalars(select(User)).all()
            self.user_chats = {u.id: u.telegram_chat_id for u in users}
            self.admins = {u.id for u in users if u.is_admin}  # só eles veem o nome do canal
            self.rules = [
                RuleSpec(
                    id=r.id,
                    keyword=r.keyword,
                    exclusions=split_terms(r.exclusions),
                    group_ids=frozenset(g.chat_id for g in r.groups),
                    item_id=r.item_id,
                    units=r.item.units if r.item else 1,
                    user_id=r.user_id,
                    variant=variant_signature(r.item.name, r.item.brand) if r.item else None,
                    max_price=r.max_price_cents or (r.item.target_cents if r.item else None),
                    item_name=r.item.name if r.item else None,
                    item_brand=r.item.brand if r.item else None,
                )
                for r in rules
            ]

    async def _on_edit(self, event) -> None:
        """Canais costumam postar e depois editar para pôr o link/preço: a oferta é registrada de novo."""
        group = self.groups.get(event.chat_id)
        if group is not None:
            await self._capture_offer(event, group, (event.raw_text or "") + _hidden_links(event))

    async def _capture_offer(self, event, group: "GroupInfo", text: str) -> None:
        """Alimenta a aba Ofertas; uma falha aqui nunca pode atrapalhar os avisos."""
        try:
            res = await asyncio.to_thread(offers.remember, group.title, text)
            msg = getattr(event, "message", event)
            photo = getattr(msg, "photo", None)
            if res and res.needs_image and photo is not None:
                data = await msg.download_media(file=bytes, thumb=offers.pick_size(photo))
                if data:
                    await asyncio.to_thread(offers.save_image, res.id, data)
        except Exception as exc:  # noqa: BLE001
            log.warning("Falha ao registrar oferta (%s)", type(exc).__name__)

    async def _on_message(self, event: events.NewMessage.Event) -> None:
        chat_id = event.chat_id
        group = self.groups.get(chat_id)
        if group is None:  # ignora tudo que não foi marcado
            return
        text = event.raw_text or ""
        links = _hidden_links(event)
        await self._capture_offer(event, group, text + links)
        hits = match_message(text, chat_id, self.rules)
        if any(h.item_id for h in hits):
            qty = pack_qty(text)  # promoção da unidade avulsa não serve para a caixa (e vice-versa)
            hits = [h for h in hits if not h.item_id
                    or (h.units == qty and not variant_conflict(h.variant, text)      # Switch 2 ≠ Switch
                        and not accessory_conflict(h.item_name, h.item_brand, text))]  # jogo PARA o console
        msg_price = price_in_text(text)
        # Teto: promoção acima do alvo/teto não avisa (sem preço na mensagem: avisa, marcando)
        hits = [h for h in hits if not (h.max_price and msg_price and msg_price > h.max_price)]
        if not hits:
            return
        if not await asyncio.to_thread(_mark_seen, chat_id, event.id):
            return  # já avisado (ex.: reentrega após reconexão)
        link = message_link(chat_id, event.id, group.username)
        by_user: dict[int | None, list] = {}
        for h in hits:  # um aviso por usuário, com todas as regras dele que bateram
            by_user.setdefault(h.user_id, []).append(h)
        store_link = offers.first_store_url(text + links)
        for user_id, user_hits in by_user.items():
            admin = user_id is None or user_id in self.admins
            shown = group.title if admin else offers.GENERIC_SOURCE  # convidado não vê de qual canal veio
            key = f"item:{user_hits[0].item_id}" if user_hits[0].item_id else f"rule:{user_hits[0].id}"
            previous = await asyncio.to_thread(repeated_promo, user_id, key, msg_price, shown)
            if previous is False:
                continue  # mesma promoção já avisada (outro grupo, últimas 24 h)
            keywords = [h.keyword for h in user_hits]
            trecho = snippet(text, user_hits[0].keyword)
            if not admin:
                trecho = offers.scrub(trecho)
            item_ids = [h.item_id for h in user_hits if h.item_id]
            extra = await asyncio.to_thread(item_lines, item_ids, msg_price) if item_ids else []
            if msg_price is None and any(h.max_price for h in user_hits):
                extra.append("💬 Preço não informado na mensagem")
            if isinstance(previous, int):
                extra.append(f"📉 Baixou de novo: antes {format_brl(previous)}")
            subject = await asyncio.to_thread(_item_name, item_ids[0]) if item_ids else keywords[0]
            title = f"🔔 {subject}" + (f" · {format_brl(msg_price)}" if msg_price else "")
            # Ao tocar: post do canal (se público, abre para qualquer um) ou a tela Avisos
            url = link if (admin and link and group.username) else "/alerts"
            out_link = link if admin else store_link
            ok = await notifier.deliver(
                user_id, notifier.format_alert(shown, keywords, trecho, out_link, extra or None,
                                               "Abrir mensagem" if admin else "Ver oferta"),
                title=title, body=f"{shown}: {trecho}" if admin else trecho, url=url, tag=key)
            await asyncio.to_thread(_save_alert, shown, keywords, trecho, out_link, ok, user_id, key, msg_price)


def _hidden_links(event) -> str:
    """Links que não aparecem no texto: palavras com hiperlink ("Comprar aqui") e botões da mensagem."""
    urls: list[str] = []
    try:
        msg = getattr(event, "message", event)
        urls += [e.url for e, _ in (msg.get_entities_text(MessageEntityTextUrl) or []) if getattr(e, "url", None)]
        urls += [b.url for row in (getattr(msg, "buttons", None) or []) for b in row if getattr(b, "url", None)]
    except Exception:  # noqa: BLE001 — mensagem estranha não pode quebrar o listener
        return ""
    return "".join(f"\n{u}" for u in dict.fromkeys(urls))


DEDUP_WINDOW = timedelta(hours=24)


def _item_name(item_id: int) -> str:
    with SessionLocal() as db:
        item = db.get(Item, item_id)
        return (item.name if item and item.name else "Seu produto")[:80]


def repeated_promo(user_id: int | None, key: str, price: int | None, group_title: str):
    """Mesma promoção (mesmo produto/regra) já avisada a este usuário nas últimas 24 h?

    Devolve None (nada parecido: avisa), o menor preço já avisado (int: avisa porque
    baixou ≥ 1%) ou False (repetida: não avisa; anota o grupo no aviso anterior)."""
    since = utcnow() - DEDUP_WINDOW
    with SessionLocal() as db:
        recent = db.scalars(select(Alert).where(Alert.user_id == user_id, Alert.dedup_key == key,
                                                Alert.created_at >= since)
                            .order_by(Alert.created_at.desc())).all()
        if not recent:
            return None
        prices = [a.price_cents for a in recent if a.price_cents]
        lowest = min(prices) if prices else None
        if price and (lowest is None or price <= lowest * 0.99):
            return lowest or None
        last = recent[0]
        groups = [g for g in (last.also_in or "").split(", ") if g]
        if group_title != last.group_title and group_title not in groups:
            last.also_in = ", ".join(groups + [group_title])[:2000]
            db.commit()
        return False


def item_lines(item_ids: list[int], msg_price: int | None) -> list[str]:
    """Linhas do aviso que comparam a promoção com o produto acompanhado."""
    e = html.escape
    lines = []
    with SessionLocal() as db:
        for iid in dict.fromkeys(item_ids):
            item = db.get(Item, iid)
            if item is None:
                continue
            lines.append(f"🎯 Seu produto: <b>{e(item.name or 'produto acompanhado')}</b>")
            best = item.best_offer()
            if msg_price:
                notes = []
                if item.target_cents and msg_price <= item.target_cents:
                    notes.append("<b>abaixo do seu alvo!</b>")
                if best and msg_price < best.current_cents:
                    notes.append(f"melhor que nas lojas ({format_brl(best.current_cents)} na {e(best.store.name)})")
                lines.append(f"💰 Preço na mensagem: <b>{format_brl(msg_price)}</b>"
                             + (" — " + "; ".join(notes) if notes else ""))
            elif best:
                lines.append(f"🏷️ Melhor preço nas lojas: {format_brl(best.current_cents)} na {e(best.store.name)}")
    return lines


def _mark_seen(chat_id: int, message_id: int) -> bool:
    """True se a mensagem é nova; False se já tinha sido registrada."""
    with SessionLocal() as db:
        db.add(SeenMessage(chat_id=chat_id, message_id=message_id))
        try:
            db.commit()
            return True
        except IntegrityError:
            db.rollback()
            return False


def _save_alert(title: str, keywords: list[str], trecho: str, link: str | None, ok: bool,
                user_id: int | None = None, key: str | None = None, price: int | None = None) -> None:
    with SessionLocal() as db:
        db.add(Alert(user_id=user_id, group_title=title, keywords=", ".join(keywords), snippet=trecho,
                     link=link, delivered=ok, dedup_key=key, price_cents=price))
        db.commit()


def _sync_groups(found: dict[int, tuple[str, str | None, str]]) -> None:
    with SessionLocal() as db:
        existing = {g.chat_id: g for g in db.scalars(select(TgGroup))}
        for chat_id, (title, username, kind) in found.items():
            g = existing.pop(chat_id, None)
            if g is None:
                db.add(TgGroup(chat_id=chat_id, title=title, username=username, kind=kind))
            else:
                g.title, g.username, g.kind = title, username, kind
        for gone in existing.values():  # saiu do grupo
            db.delete(gone)
        db.commit()


manager = TelegramManager()
