"""Motor de regras: funções puras, sem banco nem rede (fácil de testar)."""

import re
import unicodedata
from dataclasses import dataclass, field
from functools import lru_cache


@dataclass(frozen=True)
class RuleSpec:
    id: int
    keyword: str
    exclusions: tuple[str, ...] = ()
    group_ids: frozenset[int] = field(default_factory=frozenset)  # vazio = todos
    active: bool = True
    item_id: int | None = None  # regra de um produto acompanhado
    units: int = 1              # unidades por embalagem do produto (caixa com 12 = 12)
    user_id: int | None = None  # dono da regra (quem recebe o aviso)
    variant: tuple | None = None  # (linha, número, modificadores) do produto: Switch ≠ Switch 2
    item_name: str | None = None  # para recusar jogo/acessório PARA o produto
    item_brand: str | None = None


def _norm_char(ch: str) -> str:
    decomposed = unicodedata.normalize("NFKD", ch.lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def normalize(text: str) -> str:
    """Minúsculas, sem acentos e com espaços colapsados."""
    return re.sub(r"\s+", " ", "".join(_norm_char(c) for c in text)).strip()


def _normalize_with_map(text: str) -> tuple[str, list[int]]:
    """Normaliza sem colapsar espaços e devolve, para cada caractere do
    resultado, o índice correspondente no texto original."""
    out: list[str] = []
    index: list[int] = []
    for i, ch in enumerate(text):
        n = _norm_char(ch)
        out.append(n)
        index.extend([i] * len(n))
    return "".join(out), index


def split_terms(raw: str) -> tuple[str, ...]:
    return tuple(t for t in (normalize(p) for p in raw.split(",")) if t)


@lru_cache(maxsize=1024)
def term_pattern(term: str) -> re.Pattern[str]:
    """Termo como palavra/frase inteira. Espaços casam com qualquer espaço;
    '*' no fim vira busca por prefixo (ex.: 'notebook*')."""
    term = normalize(term)
    prefix = term.endswith("*")
    term = term.rstrip("*").strip()
    # "16gb" também casa com "16 GB" (número e unidade separados por espaço)
    body = r"\s+".join(re.sub(r"(?<=\d)(?=[a-z])", r"\\s*", re.escape(w)) for w in term.split(" "))
    tail = "" if prefix else r"(?!\w)"
    return re.compile(rf"(?<!\w){body}{tail}")


def _found(term: str, norm_text: str) -> re.Match[str] | None:
    if not term.rstrip("*").strip():
        return None
    return term_pattern(term).search(norm_text)


def keyword_terms(keyword: str) -> list[str]:
    """'galaxy + a27 + 256gb' -> ['galaxy', 'a27', '256gb']: todos precisam aparecer, em
    qualquer ordem. O separador é ' + ' (com espaços), para não quebrar termos como 'c++'."""
    parts = [t.strip() for t in keyword.split(" + ") if t.strip().rstrip("*").strip()]
    return parts or [keyword]


def _keyword_found(keyword: str, norm_text: str) -> re.Match[str] | None:
    """Primeira ocorrência se todos os termos aparecem; None se faltar algum."""
    first = None
    for term in keyword_terms(keyword):
        m = _found(term, norm_text)
        if m is None:
            return None
        first = first or m
    return first


def rule_matches(rule: RuleSpec, norm_text: str, chat_id: int) -> bool:
    if not rule.active:
        return False
    if rule.group_ids and chat_id not in rule.group_ids:
        return False
    if not _keyword_found(rule.keyword, norm_text):
        return False
    return not any(_found(ex, norm_text) for ex in rule.exclusions)


def match_message(text: str, chat_id: int, rules: list[RuleSpec]) -> list[RuleSpec]:
    """Regras que disparam para a mensagem (lista vazia = nenhum aviso)."""
    if not text:
        return []
    norm, _ = _normalize_with_map(text)
    return [r for r in rules if rule_matches(r, norm, chat_id)]


def snippet(text: str, keyword: str, width: int = 280) -> str:
    """Trecho do texto original centrado na primeira ocorrência da palavra."""
    flat = re.sub(r"\s+", " ", text).strip()
    if len(flat) <= width:
        return flat
    norm, index = _normalize_with_map(flat)
    m = _keyword_found(keyword, norm)
    center = index[m.start()] if m else 0
    start = max(0, min(center - width // 3, len(flat) - width))
    end = start + width
    return ("…" if start else "") + flat[start:end].strip() + ("…" if end < len(flat) else "")


def message_link(chat_id: int, message_id: int, username: str | None = None) -> str | None:
    """Link t.me da mensagem. Grupos básicos (não-supergrupos) não têm link."""
    if username:
        return f"https://t.me/{username}/{message_id}"
    s = str(chat_id)
    if s.startswith("-100"):
        return f"https://t.me/c/{s[4:]}/{message_id}"
    return None
