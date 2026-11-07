# MeuPrecinho

(código e pacote Python: `price_watch`)

Web app pessoal (PWA, mobile-first) que:

1. acompanha o **preço de produtos** nas lojas que você escolher e avisa quando cai;
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

## Contas (lançamento fechado)

- **Administrador**: quem já usava o app (ou quem rodar `python -m price_watch.cli set-password
  NOME`). Só ele vê Grupos, Lojas, pareamento do Telegram, Mercado Livre, coletor e **Convites**.
- **Convites**: em *Conta → Convites*, crie um link por pessoa (`/convite/<código>`), que vale para
  uma conta só. A pessoa escolhe usuário e senha e já entra.
- **Avisos por usuário**: em *Conta → Conectar Telegram*, o link `t.me/<bot>?start=<código>` liga a
  conversa da pessoa ao bot (`bot.py`, long polling); `/parar` desliga. Cada usuário tem os seus
  produtos, regras e avisos; os grupos lidos são os do administrador, e cada mensagem é cruzada com
  as regras de todos, com um aviso por pessoa. Sem Telegram conectado, o aviso fica só no app.
- O `CHAT_ID` do `.env` vira a conversa do administrador na primeira inicialização.
- **Onde receber** (*Conta*): **App** (notificação do celular via Web Push, `push.py`; padrão de
  contas novas), **Telegram** ou **Ambos**. "Ativar neste celular" registra o aparelho; chaves VAPID
  geradas e guardadas no banco (`VAPID_SUBJECT` opcional no `.env`). Requer `pip install -r
  requirements.txt` (pywebpush) e HTTPS.
- **Teto nas promoções dos grupos**: regra de produto avisa só até o preço-alvo; regra manual tem
  "avisar só até R$". Mensagem sem preço avisa, marcando "preço não informado". O preço da mensagem
  ignora parcela, cupom, "R$ X OFF", cashback, economia e frete.
- **Sem repetidas**: a mesma promoção (mesmo produto/regra) em outros grupos nas últimas 24 h não
  avisa de novo, salvo se o preço cair 1% ou mais; a tela Avisos mostra "também em: …".

- **Atualização pelo app** (*Conta → Atualização do app*, só administrador, `updater.py`): mostra a
  versão e as novidades do repositório; *Atualizar agora* faz `git pull`, instala dependências se
  mudaram, confere que o código carrega (senão volta à versão anterior) e encerra o processo para o
  systemd religar. Requer `Restart=always` (ou `on-failure`) no serviço; a tela mostra o comando de
  uma vez só se faltar.

## Preços (fase 3)

- **Produtos**: cole o link do produto ou, com o app instalado, use *Compartilhar → MeuPrecinho*
  no app da loja. Preço-alvo é opcional: com alvo, avisa ao ficar abaixo dele (e de novo se cair
  mais); sem alvo, avisa quando bate o menor preço já visto. Produto indisponível não gera aviso.
- **Lojas**: sugestões das principais lojas brasileiras; uma loja também é criada sozinha ao
  cadastrar um produto. Cada loja define o intervalo entre consultas e o modo de leitura.
- **Como o preço é lido**: seletor CSS da loja (se você definir) → dados estruturados
  schema.org (JSON-LD) → metatags → seletores conhecidos (Amazon, Mercado Livre).
- **Bloqueios**: várias lojas barram robôs vindos de servidores na nuvem (captcha ou HTTP 403).
  O produto mostra "erro na leitura" com o motivo. Na VM funcionaram KaBuM e Americanas.
- **Coletor de casa**: para essas lojas, a loja fica com *Onde ler = Celular de casa* e as páginas
  são baixadas por `coletor.py`, que roda no Termux de um Android no Wi-Fi de casa (IP
  residencial) e devolve o HTML ao servidor, que extrai o preço. Pela internet de casa passaram
  Amazon, Casas Bahia e Carrefour; Magalu, Pichau e Mercado Livre bloquearam até lá.
  Instalação: aba **Conta → Coletor de casa** (comando pronto com endereço e token).
  API: `GET /api/coletor/jobs` e `POST /api/coletor/result` (token no cabeçalho
  `Authorization: Bearer`). Se o coletor sumir por 60 min, o bot avisa.
- **Mercado Livre (API oficial)**: crie um app em developers.mercadolivre.com.br, coloque
  `ML_CLIENT_ID`/`ML_CLIENT_SECRET` no `.env`, cadastre no app o endereço de redirecionamento
  mostrado em **Conta → Mercado Livre** e toque em *Conectar*. Os tokens ficam no banco e são
  renovados sozinhos. Funciona com links de anúncio (`MLB-123…`) e de catálogo (`/p/MLB123…`).
- **Várias lojas por produto**: cada produto (item) tem ofertas em lojas diferentes; o alvo vale
  para o melhor preço entre elas e o aviso diz em qual loja está. *Adicionar outra loja* à mão ou
  pela **busca automática** (`discovery.py`), que roda na 1ª leitura e pelo botão *Buscar em
  outras lojas*: Americanas (busca pública VTEX, por EAN ou termo), KaBuM (página de busca) e
  Mercado Livre (catálogo da API, por EAN). EAN ou código de modelo iguais = oferta adicionada;
  só o nome = sugestão para confirmar.
- **Zoom** (`zoom.py`): a busca também acha o produto no Zoom (página do produto ou busca
  filtrada); de hora em hora, cada loja de lá vira uma oferta "via Zoom" que conta para o melhor
  preço e os avisos (inclui lojas que bloqueiam leitura direta, como Magalu e Amazon). Seminovos e
  usados ficam de fora; lojas já lidas direto têm prioridade. No *Comprar*, ofertas do Zoom na
  Amazon/Mercado Livre seguem o redirecionamento até a loja e recebem o nosso código de afiliado.
- **Relatório da busca**: a tela do produto mostra, loja por loja, o que a última busca achou ou
  por que falhou.
- **Produto também no Telegram**: cada produto gera uma regra (`item_rules.py`, ex.:
  `galaxy + a27 + 256gb`) com exclusões de acessórios/usados; o aviso do grupo cita o produto,
  o preço da mensagem e se está abaixo do alvo ou melhor que nas lojas.
- **Links de compra**: guardados limpos e gerados com o código de afiliado do app
  (`AMAZON_TAG`, `ML_MATT_WORD`/`ML_MATT_TOOL`) no botão *Comprar* (`/r/<id>`, conta cliques).
- **Modo Navegador (opcional)**: para sites que só mostram o preço com JavaScript. Pesa na VM
  pequena; antes, crie um swap de 2 GB:
  ```bash
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile && sudo mkswap /swapfile && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
  .venv/bin/pip install -r requirements-browser.txt
  sudo .venv/bin/playwright install-deps chromium && .venv/bin/playwright install chromium
  ```
- Datas exibidas no fuso `TIMEZONE` (padrão `America/Sao_Paulo`).

## Testar

```bash
pytest -q
```

- `tests/test_rules.py`: motor de regras (normalização, palavra inteira, prefixo,
  exclusões, escopo de grupos, trecho, links, escape do HTML do bot).
- `tests/test_seen.py`: não repetir aviso.
- `tests/test_listener.py`: listener com evento falso; ignora grupo não marcado,
  aplica a regra e envia uma vez só.
- `tests/test_pricing.py`: leitura de preço (formatos em reais, JSON-LD, metatags, Amazon,
  seletor próprio) e a decisão de avisar.
- `tests/test_monitor.py`: consulta de ponta a ponta com loja falsa (histórico, aviso único,
  erros de leitura).
- `tests/test_login.py`: bloqueio após senhas erradas.
- `tests/test_multistore.py`, `tests/test_discovery.py`, `tests/test_item_rules.py`: várias
  lojas por produto, busca automática e regra do produto no Telegram.

Teste manual ponta a ponta: pareie a conta, marque um grupo seu, crie uma regra
(ex.: `teste123`) e mande essa palavra no grupo a partir de outra conta. O aviso chega
pelo bot e aparece em **Avisos**.

## Privacidade e robustez

- Logs não incluem texto de mensagens, telefone, nomes nem a URL do bot (que contém o token).
- Senha com hash scrypt; cookie de sessão assinado, `SameSite=Lax`.
- Telethon com `auto_reconnect` + watchdog no APScheduler que reconecta a cada 60 s.
- Limpeza diária: `seen_messages` > 60 dias, avisos > 90 dias, histórico de preços > 1 ano.
