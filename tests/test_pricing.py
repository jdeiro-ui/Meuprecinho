import json

import pytest

from price_watch.pricing import (
    extract,
    find_url,
    format_brl,
    match_store,
    parse_brl,
    should_notify,
)


@pytest.mark.parametrize("raw, cents", [
    ("R$ 1.299,90", 129990),
    ("R$\xa01.299,90", 129990),
    ("1299,90", 129990),
    ("1299.90", 129990),
    ("1.299", 129900),        # milhar no Brasil
    ("12.990", 1299000),
    ("1.234.567,89", 123456789),
    ("1,299.90", 129990),     # formato americano
    ("R$ 89", 8900),
    ("de R$ 99,90 por", 9990),
    (1299.9, 129990),
    (49, 4900),
    ("0", None),
    ("", None),
    (None, None),
    ("grátis", None),
])
def test_parse_brl(raw, cents):
    assert parse_brl(raw) == cents


def test_format_brl():
    assert format_brl(129990) == "R$ 1.299,90"
    assert format_brl(5) == "R$ 0,05"
    assert format_brl(123456789) == "R$ 1.234.567,89"
    assert format_brl(None) == "—"


def _ld(obj) -> str:
    return f'<script type="application/ld+json">{json.dumps(obj)}</script>'


def test_jsonld_product_com_varias_ofertas():
    html = "<html><head>" + _ld({
        "@context": "https://schema.org", "@type": "Product", "name": "Air Fryer Mondial 4L",
        "image": ["https://img/1.jpg", "https://img/2.jpg"],
        "offers": [
            {"@type": "Offer", "price": "399.90", "availability": "https://schema.org/InStock"},
            {"@type": "Offer", "price": "379.00", "availability": "https://schema.org/InStock"},
        ],
    }) + "</head></html>"
    ex = extract(html, "https://loja.com.br/p/1")
    assert ex.price == 37900
    assert ex.name == "Air Fryer Mondial 4L"
    assert ex.image == "https://img/1.jpg"
    assert ex.available is True


def test_jsonld_em_graph_com_aggregate_offer_e_esgotado():
    html = _ld({"@graph": [
        {"@type": "WebPage", "name": "x"},
        {"@type": ["Product"], "name": "SSD 1TB", "image": {"url": "https://img/s.jpg"},
         "offers": {"@type": "AggregateOffer", "lowPrice": 349, "highPrice": 420,
                    "availability": "http://schema.org/OutOfStock"}},
    ]})
    ex = extract(html, "https://x.com.br/ssd")
    assert ex.price == 34900
    assert ex.image == "https://img/s.jpg"
    assert ex.available is False


def test_jsonld_invalido_e_ignorado():
    html = '<script type="application/ld+json">{quebrado</script><meta property="product:price:amount" content="59.90">'
    assert extract(html, "https://x.com.br").price == 5990


def test_metatags_e_itemprop():
    assert extract('<meta itemprop="price" content="1299.00">', "https://a.com.br").price == 129900
    assert extract('<span itemprop="price">R$ 2.499,00</span>', "https://a.com.br").price == 249900


def test_amazon_por_seletor_conhecido():
    html = """<title>Echo Dot | Amazon.com.br</title>
    <div id="corePriceDisplay_desktop_feature_div"><span class="a-price"><span class="a-offscreen">R$ 379,05</span></span></div>"""
    ex = extract(html, "https://www.amazon.com.br/dp/B09B8V1LZ3")
    assert ex.price == 37905
    assert ex.name == "Echo Dot"  # sufixo " | Loja" removido


def test_seletor_do_usuario_tem_prioridade():
    html = _ld({"@type": "Product", "name": "TV", "offers": {"price": 2000}}) + '<b class="pix">R$ 1.799,00</b>'
    assert extract(html, "https://x.com.br", ".pix").price == 179900
    assert extract(html, "https://x.com.br", "seletor[[invalido").price == 200000


def test_sem_preco_usa_titulo_e_imagem():
    html = '<meta property="og:title" content="  Notebook   Gamer "><meta property="og:image" content="https://i/n.png">'
    ex = extract(html, "https://x.com.br")
    assert ex.price is None
    assert ex.name == "Notebook Gamer"
    assert ex.image == "https://i/n.png"


class TestShouldNotify:
    def test_com_alvo_avisa_ao_cruzar_e_de_novo_so_se_cair_mais(self):
        assert should_notify(9000, True, 10000, None, None) == "abaixo do seu preço-alvo"
        assert should_notify(9000, True, 10000, 9000, 9000) is None
        assert should_notify(8500, True, 10000, 9000, 9000) == "abaixo do seu preço-alvo"
        assert should_notify(11000, True, 10000, 9000, None) is None

    def test_sem_alvo_avisa_no_menor_preco(self):
        assert should_notify(10000, True, None, None, None) is None  # primeira leitura
        assert should_notify(9500, True, None, 10000, None) == "menor preço já visto"
        assert should_notify(10000, True, None, 10000, None) is None

    def test_indisponivel_ou_sem_preco_nao_avisa(self):
        assert should_notify(5000, False, 10000, None, None) is None
        assert should_notify(None, True, 10000, None, None) is None


def test_find_url_em_texto_compartilhado():
    assert find_url("Olha isso! https://amzn.to/3abc). Barato") == "https://amzn.to/3abc"
    assert find_url("sem link") is None
    assert find_url(None) is None


def test_match_store():
    domains = ["mercadolivre.com.br", "amazon.com.br", "livre.com.br"]
    assert match_store("produto.mercadolivre.com.br", domains) == "mercadolivre.com.br"
    assert match_store("www.amazon.com.br", domains) == "amazon.com.br"
    assert match_store("notamazon.com.br", domains) is None


def test_nome_mantem_codigo_do_modelo():
    html = '<meta property="og:title" content="Kit Starlink Mini X, Branca - 02535308-501">'
    assert extract(html, "https://x.com.br").name == "Kit Starlink Mini X, Branca - 02535308-501"
