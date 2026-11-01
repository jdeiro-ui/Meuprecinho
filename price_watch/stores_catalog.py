"""Lojas sugeridas na tela Lojas. "browser" = sites que só mostram o preço com
JavaScript (exigem Playwright). As notas avisam de bloqueios a robôs vistos em
testes a partir de servidores na nuvem (o resultado pode variar com o IP)."""

CATALOG = [
    {"name": "Amazon", "domain": "amazon.com.br", "read_mode": "http", "note": "costuma pedir captcha a servidores"},
    {"name": "Mercado Livre", "domain": "mercadolivre.com.br", "read_mode": "http", "note": "bloqueia servidores; leitura via API virá na fase 4"},
    {"name": "Magalu", "domain": "magazineluiza.com.br", "read_mode": "http", "note": "bloqueou nos testes"},
    {"name": "KaBuM!", "domain": "kabum.com.br", "read_mode": "http", "note": ""},
    {"name": "Americanas", "domain": "americanas.com.br", "read_mode": "http", "note": ""},
    {"name": "Casas Bahia", "domain": "casasbahia.com.br", "read_mode": "http", "note": "bloqueou nos testes"},
    {"name": "Ponto", "domain": "pontofrio.com.br", "read_mode": "http", "note": "bloqueou nos testes"},
    {"name": "Carrefour", "domain": "carrefour.com.br", "read_mode": "http", "note": "bloqueou nos testes"},
    {"name": "Fast Shop", "domain": "fastshop.com.br", "read_mode": "http", "note": ""},
    {"name": "Submarino", "domain": "submarino.com.br", "read_mode": "http", "note": ""},
    {"name": "Pichau", "domain": "pichau.com.br", "read_mode": "http", "note": "bloqueou nos testes"},
    {"name": "Terabyte", "domain": "terabyteshop.com.br", "read_mode": "http", "note": "bloqueou nos testes"},
    {"name": "Netshoes", "domain": "netshoes.com.br", "read_mode": "http", "note": "bloqueou nos testes"},
    {"name": "Leroy Merlin", "domain": "leroymerlin.com.br", "read_mode": "http", "note": "bloqueou nos testes"},
    {"name": "Shopee", "domain": "shopee.com.br", "read_mode": "browser", "note": "costuma bloquear robôs"},
    {"name": "AliExpress", "domain": "aliexpress.com", "read_mode": "browser", "note": "costuma bloquear robôs"},
]

BY_DOMAIN = {s["domain"]: s for s in CATALOG}
