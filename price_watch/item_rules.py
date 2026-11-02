"""Busca do produto nos grupos do Telegram: gera e mantém a regra de cada produto."""

import re

from sqlalchemy import select

from .models import Item, Rule
from .rules import normalize

# Palavras que não ajudam a reconhecer o produto numa promoção
_GENERIC = set("""
kit novo nova original oficial lacrado garantia nf com sem de da do das dos para e em o a os as
por pra no na nos nas um uma the and with for smartphone celular aparelho cor preto preta branco
branca prata azul rosa verde vermelho vermelha cinza dourado dourada grafite bege lilas roxo
masculino masculina feminino feminina unissex infantil adulto tamanho unidade unidades un pacote
combo promocao oferta versao modelo produto smart polegadas pol tela ram memoria interna
""".split())
_GENERIC_CODES = {"4g", "5g", "4k", "8k", "2k", "hd", "fhd", "uhd", "110v", "127v", "220v",
                  "bivolt", "wifi", "wifi5", "wifi6", "usb", "usbc", "nfc", "lte"}
_CUT = re.compile(r",| - | \| |\(|/ ")

# Exclusões padrão: evitam avisos de acessórios e usados do mesmo produto
DEFAULT_EXCLUSIONS = "capa, capinha, pelicula, case, usado, seminovo, recondicionado"


def keyword_for(name: str | None, brand: str | None = None) -> str | None:
    """Palavras que uma promoção desse produto costuma ter, no formato 'a + b + c'.

    Ex.: 'Smartphone Samsung Galaxy A27 5G 256GB 8GB RAM' -> 'galaxy + a27 + 256gb'."""
    if not name:
        return None
    main = _CUT.split(name, maxsplit=1)[0]
    tokens = re.findall(r"[a-z0-9]+", normalize(main))
    brand_n = normalize(brand or "")
    codes = [t for t in tokens if re.search(r"\d", t) and re.search(r"[a-z]", t)
             and t not in _GENERIC_CODES][:2]
    words = [t for t in tokens if t.isalpha() and len(t) > 1 and t not in _GENERIC and t != brand_n]
    n_words = {0: 3, 1: 2}.get(len(codes), 1)  # menos códigos, mais palavras
    terms = list(dict.fromkeys(words[:n_words] + codes))
    if len(terms) < 2 and brand_n and brand_n not in terms:
        terms.insert(0, brand_n)
    if len(terms) < 2 and not codes:
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
