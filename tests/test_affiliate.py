import pytest
from fastapi.testclient import TestClient

from price_watch import affiliate, main
from price_watch.config import get_settings
from price_watch.db import SessionLocal, init_db
from price_watch.models import Product, Store


@pytest.fixture
def codes(monkeypatch):
    s = get_settings()
    monkeypatch.setattr(s, "AMAZON_TAG", "meuprecinho-20")
    monkeypatch.setattr(s, "ML_MATT_WORD", "jldeiro")
    monkeypatch.setattr(s, "ML_MATT_TOOL", "75607604")


@pytest.mark.parametrize("raw, clean", [
    ("https://www.amazon.com.br/Echo-Dot/dp/b09b8v1lz3/ref=sr_1_1?keywords=echo&tag=outro-20&th=1",
     "https://www.amazon.com.br/dp/B09B8V1LZ3"),
    ("https://amazon.com.br/gp/product/B0ABCDEFGH?psc=1", "https://www.amazon.com.br/dp/B0ABCDEFGH"),
    ("https://produto.mercadolivre.com.br/MLB-123456789-fone-_JM?matt_word=outro&matt_tool=1&searchVariation=9#reco",
     "https://produto.mercadolivre.com.br/MLB-123456789-fone-_JM?searchVariation=9"),
    ("https://www.kabum.com.br/produto/1?utm_source=grupo&utm_medium=x&cor=preto",
     "https://www.kabum.com.br/produto/1?cor=preto"),
])
def test_clean_url_remove_codigos_de_terceiros(raw, clean):
    assert affiliate.clean_url(raw) == clean


def test_buy_url_aplica_codigo_do_app(codes):
    assert affiliate.buy_url("https://www.amazon.com.br/x/dp/B09B8V1LZ3?tag=outro-20") == \
        "https://www.amazon.com.br/dp/B09B8V1LZ3?tag=meuprecinho-20"
    assert affiliate.buy_url("https://produto.mercadolivre.com.br/MLB-123456789?matt_word=outro") == \
        "https://produto.mercadolivre.com.br/MLB-123456789?matt_word=jldeiro&matt_tool=75607604"
    assert affiliate.buy_url("https://www.kabum.com.br/produto/1?utm_source=x") == "https://www.kabum.com.br/produto/1"


def test_sem_codigo_configurado_usa_link_limpo():
    assert affiliate.buy_url("https://www.amazon.com.br/dp/B09B8V1LZ3?tag=outro-20") == \
        "https://www.amazon.com.br/dp/B09B8V1LZ3"


def test_botao_comprar_conta_clique_e_exige_assinatura(codes):
    init_db()
    with SessionLocal() as db:
        st = db.query(Store).filter_by(domain="amazon.com.br").first() or Store(name="Amazon", domain="amazon.com.br")
        p = Product(store=st, url="https://www.amazon.com.br/dp/B0CLICK001")
        db.add(p)
        db.commit()
        pid = p.id
    c = TestClient(main.app)
    assert c.get(f"/r/{pid}?s=errada", follow_redirects=False).status_code == 404
    r = c.get(f"/r/{pid}?s={affiliate.click_sig(pid)}", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "https://www.amazon.com.br/dp/B0CLICK001?tag=meuprecinho-20"
    with SessionLocal() as db:
        assert db.get(Product, pid).clicks == 1
