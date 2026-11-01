"""Lojas sugeridas na tela Lojas.

- read_mode "browser": sites que só mostram o preço com JavaScript (Playwright).
- via "home": a loja bloqueia servidores na nuvem; a página é baixada pelo coletor
  no celular de casa (IP residencial). Baseado em testes reais de nov/2026.
"""

CATALOG = [
    {"name": "Amazon", "domain": "amazon.com.br", "read_mode": "http", "via": "home", "note": ""},
    {"name": "KaBuM!", "domain": "kabum.com.br", "read_mode": "http", "via": "vm", "note": ""},
    {"name": "Americanas", "domain": "americanas.com.br", "read_mode": "http", "via": "vm", "note": ""},
    {"name": "Casas Bahia", "domain": "casasbahia.com.br", "read_mode": "http", "via": "home", "note": ""},
    {"name": "Carrefour", "domain": "carrefour.com.br", "read_mode": "http", "via": "home", "note": ""},
    {"name": "Ponto", "domain": "pontofrio.com.br", "read_mode": "http", "via": "home", "note": ""},
    {"name": "Fast Shop", "domain": "fastshop.com.br", "read_mode": "http", "via": "vm", "note": ""},
    {"name": "Submarino", "domain": "submarino.com.br", "read_mode": "http", "via": "vm", "note": ""},
    {"name": "Terabyte", "domain": "terabyteshop.com.br", "read_mode": "http", "via": "home", "note": ""},
    {"name": "Netshoes", "domain": "netshoes.com.br", "read_mode": "http", "via": "home", "note": ""},
    {"name": "Leroy Merlin", "domain": "leroymerlin.com.br", "read_mode": "http", "via": "home", "note": ""},
    {"name": "Magalu", "domain": "magazineluiza.com.br", "read_mode": "http", "via": "home",
     "note": "bloqueou até pela internet de casa"},
    {"name": "Pichau", "domain": "pichau.com.br", "read_mode": "http", "via": "home",
     "note": "bloqueou até pela internet de casa"},
    {"name": "Mercado Livre", "domain": "mercadolivre.com.br", "read_mode": "http", "via": "home",
     "note": "bloqueia; leitura pela API oficial virá na fase 4"},
    {"name": "Shopee", "domain": "shopee.com.br", "read_mode": "browser", "via": "vm", "note": "costuma bloquear robôs"},
    {"name": "AliExpress", "domain": "aliexpress.com", "read_mode": "browser", "via": "vm", "note": "costuma bloquear robôs"},
]

BY_DOMAIN = {s["domain"]: s for s in CATALOG}
HOME_DOMAINS = [s["domain"] for s in CATALOG if s["via"] == "home"]
