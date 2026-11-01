"""Baixa a página do produto: httpx (leve) ou Playwright (sites com JavaScript)."""

import asyncio

import httpx

HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.6",
}
_BLOCK_MARKERS = ("captcha", "robot check", "are you a human", "access denied", "não é um robô",
                  "suspicious-traffic", "account-verification")


class FetchError(Exception):
    """Erro com mensagem curta, mostrada na tela do produto."""


def check_response(status: int, html: str) -> None:
    if status in (403, 429, 503):
        raise FetchError(f"a loja bloqueou a leitura (HTTP {status})")
    head = html[:20000].lower()
    if any(m in head for m in _BLOCK_MARKERS) and "application/ld+json" not in head:
        raise FetchError("a loja pediu captcha (bloqueio anti-robô)")
    if status == 404:
        raise FetchError("página não encontrada (o produto pode ter saído do ar)")
    if status >= 400:
        raise FetchError(f"a loja respondeu HTTP {status}")


async def fetch_http(url: str) -> str:
    async with httpx.AsyncClient(headers=HEADERS, follow_redirects=True, timeout=25) as c:
        try:
            r = await c.get(url)
        except httpx.HTTPError as e:
            raise FetchError(f"sem resposta da loja ({type(e).__name__})") from e
    check_response(r.status_code, r.text)
    return r.text


async def fetch_browser(url: str) -> str:
    try:
        from playwright.async_api import async_playwright
    except ImportError as e:
        raise FetchError("modo Navegador requer o Playwright instalado na VM") from e
    async with async_playwright() as pw:
        try:
            browser = await pw.chromium.launch(args=["--disable-dev-shm-usage"])
        except Exception as e:  # noqa: BLE001 — geralmente o Chromium não foi baixado
            raise FetchError("navegador não instalado na VM (rode: playwright install chromium)") from e
        try:
            page = await browser.new_page(user_agent=HEADERS["User-Agent"], locale="pt-BR")
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=45000)
            await asyncio.sleep(3)  # tempo para o JavaScript montar o preço
            html = await page.content()
            check_response(resp.status if resp else 200, html)
            return html
        except FetchError:
            raise
        except Exception as e:  # noqa: BLE001
            raise FetchError(f"falha no navegador ({type(e).__name__})") from e
        finally:
            await browser.close()


async def fetch(url: str, mode: str) -> str:
    return await (fetch_browser(url) if mode == "browser" else fetch_http(url))
