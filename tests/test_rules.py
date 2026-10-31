import pytest

from price_watch.notifier import format_alert
from price_watch.rules import (
    RuleSpec,
    match_message,
    message_link,
    normalize,
    snippet,
    split_terms,
)

G1, G2 = -1001111, -1002222


def rule(keyword, exclusions="", groups=(), active=True, id=1):
    return RuleSpec(id=id, keyword=keyword, exclusions=split_terms(exclusions),
                    group_ids=frozenset(groups), active=active)


def hits(text, rules, chat=G1):
    return [r.id for r in match_message(text, chat, rules)]


@pytest.mark.parametrize("raw, expected", [
    ("Promoção AÇÃO", "promocao acao"),
    ("  Café\n\t com   LEITE ", "cafe com leite"),
    ("São João", "sao joao"),
    ("é", "e"),  # acento combinante separado
    ("ＰＳ５", "ps5"),  # largura total -> ASCII
])
def test_normalize(raw, expected):
    assert normalize(raw) == expected


def test_match_ignora_caixa_e_acentos():
    assert hits("Promoção de AIR FRYER hoje!", [rule("air fryer")]) == [1]
    assert hits("cafeteira elétrica", [rule("Elétrica")]) == [1]
    assert hits("cafeteira eletrica", [rule("elétrica")]) == [1]


def test_frase_aceita_quebra_de_linha_entre_palavras():
    assert hits("Air\nFryer Mondial", [rule("air fryer")]) == [1]


def test_palavra_inteira():
    assert hits("ps55 nao existe", [rule("ps5")]) == []
    assert hits("PS5 slim por R$ 3.000", [rule("ps5")]) == [1]
    assert hits("(ps5)", [rule("ps5")]) == [1]
    assert hits("tvbox", [rule("tv")]) == []


def test_prefixo_com_asterisco():
    r = [rule("notebook*")]
    assert hits("Notebooks gamer em oferta", r) == [1]
    assert hits("meu notebook", r) == [1]
    assert hits("ultranotebook", r) == []


def test_exclusoes():
    r = [rule("iphone", exclusions="usado, capinha, seminovo")]
    assert hits("iPhone 15 novo lacrado", r) == [1]
    assert hits("iPhone 15 USADO", r) == []
    assert hits("Capinha para iphone", r) == []
    assert hits("iphone semi-novo", r) == [1]  # exclusão também é palavra inteira


def test_exclusao_com_acento():
    assert hits("Monitor com defeito", [rule("monitor", "Defeito")]) == []
    assert hits("Monitor com DEFEITO", [rule("monitor", "défeito")]) == []


def test_regra_inativa_nao_dispara():
    assert hits("air fryer", [rule("air fryer", active=False)]) == []


def test_escopo_de_grupos():
    r = [rule("ssd", groups={G2})]
    assert hits("SSD 1TB", r, chat=G1) == []
    assert hits("SSD 1TB", r, chat=G2) == [1]
    # sem grupos: vale para qualquer grupo monitorado
    assert hits("SSD 1TB", [rule("ssd")], chat=G2) == [1]


def test_varias_regras():
    rules = [rule("ssd", id=1), rule("nvme", id=2), rule("hd", id=3)]
    assert hits("SSD NVMe 2TB", rules) == [1, 2]


def test_texto_vazio_e_palavra_vazia():
    assert hits("", [rule("x")]) == []
    assert hits("qualquer coisa", [rule("  ")]) == []
    assert hits("qualquer coisa", [rule("*")]) == []


def test_caracteres_especiais_na_palavra():
    assert hits("Placa RTX 4060 Ti (8GB)", [rule("4060 ti")]) == [1]
    assert hits("cupom C++ 10%", [rule("c++")]) == [1]
    assert hits("preço R$ 99,90", [rule("r$ 99")]) == [1]


def test_split_terms():
    assert split_terms(" Usado, , DEFEITO ,  caixa aberta ") == ("usado", "defeito", "caixa aberta")
    assert split_terms("") == ()


def test_snippet_curto_mantem_texto():
    assert snippet("Air fryer\n  barata", "air fryer") == "Air fryer barata"


def test_snippet_longo_centraliza_na_palavra():
    texto = "a " * 300 + "Promoção da AIR FRYER Mondial" + " b" * 300
    s = snippet(texto, "air fryer", width=120)
    assert "AIR FRYER" in s
    assert s.startswith("…") and s.endswith("…")
    assert len(s) <= 122


def test_snippet_acento_mantem_original():
    texto = "x " * 200 + "Ação imperdível de Café" + " y" * 200
    assert "Café" in snippet(texto, "cafe", width=60)


def test_links():
    assert message_link(-1001234567, 42, "promos") == "https://t.me/promos/42"
    assert message_link(-1001234567, 42) == "https://t.me/c/1234567/42"
    assert message_link(-4567, 42) is None  # grupo básico não tem link


def test_format_alert_escapa_html():
    msg = format_alert("<Grupo & Cia>", ["ps5"], "<b>oi</b>", "https://t.me/c/1/2")
    assert "&lt;Grupo &amp; Cia&gt;" in msg
    assert "&lt;b&gt;oi&lt;/b&gt;" in msg
    assert 'href="https://t.me/c/1/2"' in msg
