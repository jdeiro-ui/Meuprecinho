"""Busca do produto nos grupos do Telegram: gera e mantém a regra de cada produto."""

import re

from sqlalchemy import select

from .models import Item, Rule
from .rules import normalize

# Palavras que não ajudam a reconhecer o produto numa promoção
_GENERIC = set("""
kit novo nova original oficial lacrado garantia nf com sem de da do das dos para e em o a os as
por pra no na nos nas um uma the and with for aparelho cor preto preta branco branca prata azul
rosa verde vermelho vermelha cinza dourado dourada grafite bege lilas roxo amarelo laranja
masculino masculina feminino feminina unissex infantil adulto tamanho unidade unidades un pacote
combo promocao oferta versao modelo produto polegadas pol tela ram memoria interna geracao
lancamento edicao cm mm sabor sabores caixa contem com bluetooth wireless sem fio
""".split())
# Palavras de categoria: costumam abrir o nome na loja, mas quase nunca na promoção
_CATEGORY = set("""
smartphone celular barra barrinha proteina proteica suplemento fritadeira eletrica eletrico oleo
cafeteira liquidificador aspirador televisor smart tv notebook tablet leitor ereader livro digital
fone ouvido headset caixa som relogio monitor console videogame jogo cadeira geladeira refrigerador
maquina lavar micro ondas forno ventilador ar condicionado camiseta camisa tenis sapato bermuda
calca vestido perfume shampoo kit conjunto
""".split())
_GENERIC_CODES = {"4g", "5g", "4k", "8k", "2k", "hd", "fhd", "uhd", "110v", "127v", "220v",
                  "bivolt", "wifi", "wifi5", "wifi6", "usb", "usbc", "nfc", "lte"}
# Unidades que se juntam ao número: "16 GB" -> "16gb", "40 g" -> "40g"
_UNITS = {"gb", "tb", "mb", "l", "ml", "kg", "g", "mg", "w", "v", "mah", "hz", "un"}
_CUT = re.compile(r" \| |: | - ")  # sufixo da loja e descrições longas depois de ":" ou " - "

# Exclusões padrão: evitam avisos de acessórios e usados do mesmo produto
DEFAULT_EXCLUSIONS = "capa, capinha, pelicula, case, usado, seminovo, recondicionado"


def _main_part(name: str) -> str:
    """Nome sem sufixo da loja e sem a descrição depois de ':' ou ' - '. A parte depois
    da vírgula só fica se antes dela não houver o suficiente para identificar o produto."""
    main = _CUT.split(name, maxsplit=1)[0]
    head, comma, _ = main.partition(",")
    if comma:
        strong = [t for t in re.findall(r"[a-z0-9]+", normalize(head))
                  if t not in _GENERIC and t not in _CATEGORY and len(t) > 1]
        if len(strong) >= 2:
            return head
    return main


def _tokens(name: str) -> list[str]:
    raw = re.findall(r"[a-z0-9]+", normalize(_main_part(name)))[:16]
    out: list[str] = []
    for t in raw:
        if out and out[-1].isdigit() and t in _UNITS:
            out[-1] += t  # junta número e unidade
        else:
            out.append(t)
    return out


def _is_code(t: str) -> bool:
    """Código de modelo/capacidade (a27, 256gb, me29b, 40g). Códigos longos de fábrica
    (qn55qn90bagxzd) ficam de fora: as promoções quase nunca os escrevem inteiros."""
    return (t not in _GENERIC_CODES and len(t) <= 7
            and bool(re.search(r"\d", t)) and bool(re.search(r"[a-z]", t)))


def _is_strong_code(t: str) -> bool:
    """Código que sozinho identifica o modelo (a27, me29b), não uma medida (256gb, 40g)."""
    return _is_code(t) and not re.fullmatch(r"\d+[a-z]{1,3}", t)


def keyword_for(name: str | None, brand: str | None = None) -> str | None:
    """Palavras que uma promoção desse produto costuma ter, no formato 'a + b + c':
    linha/característica do produto, códigos de modelo/capacidade e a marca (quando ela
    está no nome e não há um código de modelo que já identifique o produto).

    Ex.: 'Barra de Proteína Bold Thin Cookies & Cream 40g' -> 'bold + thin + cookies + 40g';
         'Kindle 16 GB (Geração 2024)' -> 'kindle + 16gb';
         'Smartphone Samsung Galaxy A27 5G 256GB' -> 'galaxy + a27 + 256gb'."""
    if not name:
        return None
    tokens = _tokens(name)
    brand_tokens = re.findall(r"[a-z0-9]+", normalize(brand or ""))
    brand_in_name = [t for t in brand_tokens if t in tokens][:1]

    # Pula a categoria (e palavras genéricas) do começo do nome
    zone = [t for t in tokens if t not in brand_tokens]
    while zone and (zone[0] in _CATEGORY or zone[0] in _GENERIC):
        zone.pop(0)

    words = [t for t in zone if t.isalpha() and len(t) > 1 and t not in _GENERIC]
    codes = [t for t in zone if _is_code(t)]
    if words and (i := zone.index(words[0])) + 1 < len(zone):
        nxt = zone[i + 1]
        if nxt.isdigit() and len(nxt) <= 3:
            codes.insert(0, nxt)  # número da linha: iPhone 17, PlayStation 5
    codes = list(dict.fromkeys(codes))[:2]
    strong = any(_is_strong_code(t) for t in codes)

    max_words = 1 if len(codes) >= 2 else 2 if codes else 3
    if not brand_tokens:
        max_words += 1  # sem marca conhecida, a primeira palavra costuma ser a própria marca
    use_brand = brand_in_name and not strong
    chosen = set(words[:max_words]) | set(codes) | set(brand_in_name if use_brand else [])
    terms = [t for t in dict.fromkeys(tokens) if t in chosen]  # na ordem do nome
    if len(terms) < 2 and not any(_is_code(t) for t in terms):
        return None  # uma palavra só pegaria promoções demais
    return " + ".join(terms)


def sync(db, item: Item) -> bool:
    """Cria, atualiza ou remove a regra do produto. Devolve True se mudou algo."""
    rule = db.scalar(select(Rule).where(Rule.item_id == item.id))
    if not item.watch_telegram:
        if rule is None:
            return False
        db.delete(rule)
        return True
    keyword = keyword_for(item.name, item.brand)
    if rule is None:
        if not keyword:
            return False
        db.add(Rule(keyword=keyword, exclusions=DEFAULT_EXCLUSIONS, active=item.active,
                    item_id=item.id, auto=True))
        return True
    changed = False
    if rule.auto and keyword and keyword != rule.keyword:
        rule.keyword, changed = keyword, True
    if rule.active != item.active:
        rule.active, changed = item.active, True
    return changed


def resync_all() -> int:
    """Recalcula as regras automáticas de todos os produtos (ex.: após melhorar o gerador)."""
    from .db import SessionLocal

    changed = 0
    with SessionLocal() as db:
        for item in db.scalars(select(Item)).all():
            changed += sync(db, item)
        db.commit()
    return changed


def match_keyword_for(name: str | None, brand: str | None = None) -> str | None:
    """Versão para comparar títulos de produtos (lojas, Zoom): sem a marca quando o resto
    já identifica o produto, porque catálogos às vezes omitem a marca ("iPhone 17 256GB")."""
    keyword = keyword_for(name, brand)
    if not keyword:
        return None
    brand_tokens = set(re.findall(r"[a-z0-9]+", normalize(brand or "")))
    rest = [t for t in keyword.split(" + ") if t not in brand_tokens]
    if len(rest) >= 2 or any(_is_code(t) for t in rest):
        return " + ".join(rest)
    return keyword
