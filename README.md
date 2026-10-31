# price-watch

Web app pessoal (PWA, mobile-first) que:

1. guarda as **lojas** que você quer monitorar (a coleta de preços chega nas próximas fases);
2. lê **grupos/canais do Telegram** dos quais você é membro (só os marcados no app) e avisa,
   por um bot, quando uma mensagem contém suas **palavras-chave**.

Stack: FastAPI + Jinja/HTMX, SQLite (SQLAlchemy), Telethon, httpx, APScheduler.

## Rodar

```bash
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # preencha SECRET_KEY, API_ID, API_HASH, BOT_TOKEN, CHAT_ID
python -m price_watch.cli set-password admin   # cria o único usuário
uvicorn price_watch.main:app --host 0.0.0.0 --port 8000
```

- Use **um único processo** (sem `--workers`): a sessão do Telethon é um arquivo só.
- Banco e sessão ficam em `data/` (ignorado pelo git, junto com `.env` e `*.session`).
- `API_ID`/`API_HASH`: https://my.telegram.org → *API development tools*.
- `BOT_TOKEN`: crie um bot com @BotFather. `CHAT_ID`: mande uma mensagem ao bot e veja
  `https://api.telegram.org/bot<TOKEN>/getUpdates` (campo `chat.id`).

### No Android

O Chrome só oferece "Instalar app" em **HTTPS** (ou `localhost`). Exponha o app por um
proxy com TLS (Caddy, Cloudflare Tunnel, Tailscale Serve…) e defina `COOKIE_SECURE=true`.
Depois abra o endereço no Chrome → menu ⋮ → *Instalar app*.

## Uso

1. **Conta**: informe o telefone, o código recebido e, se houver, a senha 2FA.
2. **Grupos**: toque em *Atualizar lista do Telegram* e ligue a chave dos grupos a monitorar.
3. **Regras**: palavra ou frase (sem diferença de maiúsculas/acentos, palavra inteira;
   `notebook*` = prefixo), exclusões separadas por vírgula, grupos (nenhum = todos os
   monitorados) e ativo.
4. **Avisos**: histórico dos últimos avisos e botão para testar o bot.

Cada mensagem gera no máximo um aviso (tabela `seen_messages`), com nome do grupo,
trecho e link (grupos básicos, que não são supergrupos, não têm link de mensagem).

## Testar

```bash
pytest -q
```

- `tests/test_rules.py`: motor de regras (normalização, palavra inteira, prefixo,
  exclusões, escopo de grupos, trecho, links, escape do HTML do bot).
- `tests/test_seen.py`: não repetir aviso.
- `tests/test_listener.py`: listener com evento falso; ignora grupo não marcado,
  aplica a regra e envia uma vez só.

Teste manual ponta a ponta: pareie a conta, marque um grupo seu, crie uma regra
(ex.: `teste123`) e mande essa palavra no grupo a partir de outra conta. O aviso chega
pelo bot e aparece em **Avisos**.

## Privacidade e robustez

- Logs não incluem texto de mensagens, telefone, nomes nem a URL do bot (que contém o token).
- Senha com hash scrypt; cookie de sessão assinado, `SameSite=Lax`.
- Telethon com `auto_reconnect` + watchdog no APScheduler que reconecta a cada 60 s.
- Limpeza diária: `seen_messages` > 60 dias e avisos > 90 dias.
