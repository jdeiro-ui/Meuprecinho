"""Conta do usuário via Telethon: pareamento, lista de grupos e listener."""

import asyncio
import logging
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from telethon import TelegramClient, events
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    SessionPasswordNeededError,
)

from . import notifier
from .config import get_settings
from .db import SessionLocal
from .models import Alert, Rule, SeenMessage, TgGroup
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
            device_model="price-watch",
        )
        client.add_event_handler(self._on_message, events.NewMessage(incoming=True))
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
            self.rules = [
                RuleSpec(
                    id=r.id,
                    keyword=r.keyword,
                    exclusions=split_terms(r.exclusions),
                    group_ids=frozenset(g.chat_id for g in r.groups),
                )
                for r in rules
            ]

    async def _on_message(self, event: events.NewMessage.Event) -> None:
        chat_id = event.chat_id
        group = self.groups.get(chat_id)
        if group is None:  # ignora tudo que não foi marcado
            return
        text = event.raw_text or ""
        hits = match_message(text, chat_id, self.rules)
        if not hits:
            return
        if not await asyncio.to_thread(_mark_seen, chat_id, event.id):
            return  # já avisado (ex.: reentrega após reconexão)
        keywords = [h.keyword for h in hits]
        trecho = snippet(text, hits[0].keyword)
        link = message_link(chat_id, event.id, group.username)
        ok = await notifier.send(notifier.format_alert(group.title, keywords, trecho, link))
        await asyncio.to_thread(_save_alert, group.title, keywords, trecho, link, ok)


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


def _save_alert(title: str, keywords: list[str], trecho: str, link: str | None, ok: bool) -> None:
    with SessionLocal() as db:
        db.add(Alert(group_title=title, keywords=", ".join(keywords), snippet=trecho, link=link, delivered=ok))
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
