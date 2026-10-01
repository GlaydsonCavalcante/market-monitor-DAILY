"""
processor.py

Módulo de processamento para o pipeline diário:
- Decodificação e rastreio de URLs canônicas.
- Agrupamento léxico veloz via rapidfuzz (sem PyTorch/BGE-M3).
- Validação factual com rejeição estrita de barreiras antibot (Bloomberg/Cloudflare).
- Extração resiliente em cascata: Fast HTTP seguido de Playwright Stealth.
"""

import asyncio
import base64
from datetime import datetime
import os
import random
import re
import time
from typing import Dict, List, Optional, Tuple
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

from bs4 import BeautifulSoup
import googlenewsdecoder
from playwright.async_api import async_playwright
from rapidfuzz import fuzz
import requests
import trafilatura

FUSO_BRASILIA = ZoneInfo("America/Sao_Paulo")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

TAMANHO_MINIMO_TEXTO = 150

DOMINIOS_MIDIA = {
    "youtube.com", "youtu.be", "spotify.com", "open.spotify.com",
    "soundcloud.com", "vimeo.com", "globoplay.globo.com", "podcasts.apple.com"
}
DOMINIOS_FECHADOS = {
    "twitter.com", "x.com", "facebook.com", "instagram.com", "linkedin.com"
}

PADROES_ANTIBOT = [
    r"unsanctioned scraping by bots",
    r"instituted a challenge designed to keep them out",
    r"enable javascript and cookies to continue",
    r"checking your browser before accessing",
    r"attention required!? \| cloudflare",
    r"please verify you are a human",
    r"access denied \| \d+ access denied",
    r"access\s+denied",
    r"you\s+don'?t\s+have\s+permission\s+to\s+access",
    r"error\s+loading\s+chunks",
    r"ray id: [a-f0-9]{16}",
    r"incident\s+id:",
    r"pardon our interruption",
    r"verifique se você é humano",
    r"ative o javascript para continuar",
    r"acesso negado",
    r"security check to access",
    r"ddos protection by cloudflare",
    r"block details:.*incident id",
    r"perimeterx",
    r"datadome",
    r"akamai\s*ghost",
    r"403\s+forbidden",
    r"401\s+unauthorized",
    r"acceso\s+denegado",
    r"permiso\s+denegado",
    r"accès\s+refusé",
    r"zugriff\s+verweigert",
    r"página\s+não\s+encontrada",
    r"let us know you'?re not a robot",
    r"click the box below to let us know",
    r"supports javascript and cookies",
    r"bloomberg\.com subscription",
    r"why did this happen\?.*captcha",
]
REGEX_ANTIBOT = re.compile("|".join(PADROES_ANTIBOT), re.IGNORECASE)

STOPWORDS_TITULO = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with",
    "by", "from", "up", "about", "into", "over", "after", "o", "a", "os", "as", "um",
    "uma", "de", "da", "do", "em", "para", "com", "por", "sobre", "el", "la", "los",
    "las", "en", "por", "para", "con", "del", "al", "der", "die", "das", "und", "im",
    "le", "les", "des", "pour", "dans", "sur", "que", "is", "are", "was", "were"
}


def normalizar_nome_tema(tema: str) -> str:
    """Normaliza texto removendo diacríticos e gerando slug legível."""
    nfkd = unicodedata.normalize("NFKD", tema)
    sem_acento = "".join([c for c in nfkd if not unicodedata.combining(c)])
    limpo = re.sub(r"[^\w\s-]", "", sem_acento).strip().lower()
    return re.sub(r"[\s-]+", "_", limpo)


def classificar_url_terminal(url: str) -> Optional[str]:
    """Identifica páginas não textuais, redes fechadas ou redirecionamentos para raiz."""
    if not url or not isinstance(url, str):
        return None
    try:
        parsed = urllib.parse.urlparse(url.lower())
        netloc = parsed.netloc.replace("www.", "")
        if any(netloc == d or netloc.endswith("." + d) for d in DOMINIOS_MIDIA):
            return "CONTEUDO_MIDIA"
        if any(netloc == d or netloc.endswith("." + d) for d in DOMINIOS_FECHADOS):
            return "PLATAFORMA_FECHADA"
        path = parsed.path.strip("/")
        if not path or path in ["index.html", "index.php", "home", "noticias", "economia", "politica"]:
            if not parsed.query:
                return "REDIRECT_HOMEPAGE"
    except Exception:
        pass
    return None


def validar_integridade_factual(titulo: str, texto: Optional[str]) -> Tuple[bool, str]:
    """Valida volume de texto, assinaturas de bloqueio e obrigatoriedade de 2+ termos do título no corpo."""
    if not texto or len(texto.strip()) < TAMANHO_MINIMO_TEXTO:
        return False, "TEXTO_MUITO_CURTO"

    amostra = texto[:2500]
    if REGEX_ANTIBOT.search(amostra):
        return False, "ERRO_SCRAPING_BLOQUEIO_CDN"

    tokens_titulo = [
        t.lower() for t in re.findall(r"\b[a-zA-Z0-9\u00C0-\u00FF]{4,}\b", titulo or "")
        if t.lower() not in STOPWORDS_TITULO
    ]

    if len(tokens_titulo) >= 3:
        texto_lower = texto.lower()
        matches = sum(1 for token in tokens_titulo if token in texto_lower)
        if matches < 2:
            return False, "SEM_SOBREPOSICAO_TITULO_CORPO"

    return True, "APTO"


def decode_token_offline(token: str) -> Optional[str]:
    """Decodifica URL primária a partir do token Base64 do Google News."""
    try:
        padded = token + "=" * (-len(token) % 4)
        raw_bytes = base64.urlsafe_b64decode(padded)
        matches = re.findall(rb"https?://[a-zA-Z0-9_\-\.\/\?\=\&\%\#\:\@]+", raw_bytes)
        for url_bytes in matches:
            url_str = url_bytes.decode("utf-8", errors="ignore")
            if "google.com" not in url_str and "schema.org" not in url_str and len(url_str) > 15:
                return url_str
    except Exception:
        pass
    return None


def resolve_publisher_url(google_news_url: str) -> str:
    """Resolve a URL final do veículo jornalístico via decodificação offline ou RPC."""
    if not google_news_url or "news.google.com" not in google_news_url:
        return google_news_url

    clean_url = google_news_url.split("?")[0].strip()
    match = re.search(r"/articles/([^/?&]+)", clean_url)
    if match:
        extracted = decode_token_offline(match.group(1))
        if extracted and "google.com" not in extracted:
            return extracted

    try:
        res = gnewsdecoder(google_news_url)
        if isinstance(res, dict):
            decoded = res.get("decoded_url")
        elif isinstance(res, str):
            decoded = res
        else:
            decoded = None

        if decoded and decoded.startswith("http") and "news.google.com" not in decoded:
            return decoded
    except Exception as exc:
        # Não mascara em nível crítico; registra no stderr para auditoria
        sys.stderr.write(f"[WARN] Falha decodificacao RPC: {exc}\n")

    return google_news_url
    

def build_rss_query(base_term: str, excluded_terms: list, preferred_domains: list) -> str:
    """Gera string de busca combinando operadores booleanos e exclusões."""
    parts = [base_term]
    if preferred_domains:
        sites_query = " OR ".join([f"site:{domain}" for domain in preferred_domains])
        parts.append(f"({sites_query})")
    if excluded_terms:
        parts.extend([f"-{term}" for term in excluded_terms])
    return " ".join(parts)


def fetch_rss_feed(query: str, hl: str, gl: str, timeout: int = 6) -> list:
    """Extrai feeds do Google News RSS."""
    encoded_query = urllib.parse.quote(query)
    ceid = f"{hl.upper()}:{gl.upper()}"
    url = f"https://news.google.com/rss/search?q={encoded_query}&hl={hl}&gl={gl}&ceid={ceid}"
    headers = {"User-Agent": random.choice(USER_AGENTS)}
    try:
        response = requests.get(url, headers=headers, timeout=timeout)
        if response.status_code != 200:
            return []
        root = ET.fromstring(response.content)
        feed_items = []
        for item in root.findall(".//channel/item"):
            feed_items.append({
                "titulo": (item.find("title").text or "").strip(),
                "link": (item.find("link").text or "").strip(),
                "data_noticia": (item.find("pubDate").text or "").strip(),
                "fonte": (item.find("source").text or "Google News").strip() if item.find("source") is not None else "Google News",
                "snippet": (item.find("description").text or "").strip(),
            })
        return feed_items
    except Exception:
        return []


def scrape_article_text(url: str, timeout: int = 5) -> Tuple[Optional[str], str, str]:
    """Estágio 1: Extração rápida via requests e Trafilatura."""
    real_url = resolve_publisher_url(url)
    if "news.google.com" in real_url:
        return None, "FALHA_DECODIFICACAO_URL", real_url

    cat_term = classificar_url_terminal(real_url)
    if cat_term:
        return None, cat_term, real_url

    headers = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.7,es;q=0.6",
    }
    try:
        resp = requests.get(real_url, headers=headers, timeout=(2.5, float(timeout)), allow_redirects=True)
    except requests.exceptions.RequestException as e:
        return None, f"FALHA_CONEXAO_{type(e).__name__}", real_url

    if resp.status_code != 200:
        return None, f"FALHA_HTTP_{resp.status_code}", resp.url

    html_content = resp.text
    if not html_content:
        return None, "HTML_VAZIO", resp.url

    text = trafilatura.extract(html_content, include_comments=False, include_tables=False, output_format="txt")
    if not text or len(text.strip()) < TAMANHO_MINIMO_TEXTO:
        soup = BeautifulSoup(html_content, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
            tag.decompose()
        paragraphs = [p.get_text().strip() for p in soup.find_all("p") if len(p.get_text().strip()) > 35]
        text = "\n\n".join(paragraphs)

    return (text.strip() if text else None), "SUCESSO", resp.url


def cluster_articles_rapido(
    raw_articles: list,
    similarity_threshold: float = 80.0,
    prefixo_id: str = "CLUS",
) -> list:
    """Deduplicação de notícias do dia via rapidfuzz."""
    if not raw_articles:
        return []

    clusters = []
    visited = set()
    total = len(raw_articles)

    for i in range(total):
        if i in visited:
            continue
        cluster_members = [raw_articles[i]]
        visited.add(i)
        tit_i = raw_articles[i]["titulo"]

        for j in range(i + 1, total):
            if j in visited:
                continue
            tit_j = raw_articles[j]["titulo"]
            sim = fuzz.token_sort_ratio(tit_i, tit_j)
            if sim >= similarity_threshold:
                cluster_members.append(raw_articles[j])
                visited.add(j)

        primary = cluster_members[0]
        mirrors = [m["link"] for m in cluster_members[1:] if m["link"] != primary["link"]]
        clusters.append({
            "id_cluster": f"{prefixo_id}_{len(clusters) + 1:04d}",
            "tema": primary["tema"],
            "termo_origem": primary["termo_origem"],
            "titulo": primary["titulo"],
            "data_noticia": primary["data_noticia"],
            "idioma": primary["idioma"],
            "fonte_utilizada": primary["fonte"],
            "url_utilizada": primary["link"],
            "url_primaria": primary["link"],
            "urls_espelho": mirrors,
            "urls_espelho_disponiveis": mirrors,
            "urls_espelho_canonicas": [],
            "url_canonica_resolvida": None,
            "status_resolucao": "PENDENTE",
            "status_extracao": "PENDENTE",
            "texto_completo": "",
            "motivo_bloqueio": None,
            "necessita_extracao_manual": False,
            "historico_tentativas": [],
            "vetor_1024": None,
        })
    return clusters


def process_cluster_with_fallback(cluster: dict, timeout: int = 6) -> dict:
    """Tenta iterativamente extrair o texto de cada link do cluster até obter sucesso validado."""
    urls_to_try = [cluster["url_primaria"]]
    for esp in cluster.get("urls_espelho", []):
        if esp and esp not in urls_to_try:
            urls_to_try.append(esp)

    historico = list(cluster.get("historico_tentativas", []))
    espelhos_decodificados = [resolve_publisher_url(e) for e in cluster.get("urls_espelho", [])]
    titulo = cluster.get("titulo", "")
    primeira_url_resolvida = None
    ultimo_motivo = "HTTP_TIMEOUT_OU_BLOQUEIO"

    for link in urls_to_try:
        texto, status_http, final_url = scrape_article_text(link, timeout=timeout)
        if primeira_url_resolvida is None:
            primeira_url_resolvida = final_url

        if status_http != "SUCESSO" or not texto:
            ultimo_motivo = status_http
            historico.append({
                "url_original_rss": link,
                "url_canonica_decodificada": final_url,
                "status": status_http,
                "motivo": status_http,
                "timestamp": datetime.now(FUSO_BRASILIA).strftime("%Y-%m-%d %H:%M:%S"),
            })
            continue

        apto, motivo = validar_integridade_factual(titulo, texto)
        if not apto:
            ultimo_motivo = motivo
            historico.append({
                "url_original_rss": link,
                "url_canonica_decodificada": final_url,
                "status": f"DESCARTE_FACTUAL_{motivo}",
                "motivo": motivo,
                "timestamp": datetime.now(FUSO_BRASILIA).strftime("%Y-%m-%d %H:%M:%S"),
            })
            continue

        historico.append({
            "url_original_rss": link,
            "url_canonica_decodificada": final_url,
            "status": "SUCESSO",
            "motivo": None,
            "timestamp": datetime.now(FUSO_BRASILIA).strftime("%Y-%m-%d %H:%M:%S"),
        })

        cluster["url_utilizada"] = final_url
        cluster["url_canonica_resolvida"] = final_url
        cluster["urls_espelho_disponiveis"] = [u for u in espelhos_decodificados if u != final_url]
        cluster["urls_espelho_canonicas"] = [u for u in espelhos_decodificados if u != final_url and "news.google.com" not in u]
        cluster["status_resolucao"] = "RESOLVIDO"
        cluster["status_extracao"] = "SUCESSO"
        cluster["texto_completo"] = texto
        cluster["motivo_bloqueio"] = None
        cluster["necessita_extracao_manual"] = False
        cluster["historico_tentativas"] = historico
        return cluster

    cluster["url_utilizada"] = primeira_url_resolvida or cluster["url_primaria"]
    cluster["url_canonica_resolvida"] = primeira_url_resolvida or cluster["url_primaria"]
    cluster["urls_espelho_disponiveis"] = espelhos_decodificados
    cluster["urls_espelho_canonicas"] = [u for u in espelhos_decodificados if "news.google.com" not in u]
    cluster["status_resolucao"] = "FALHA"
    cluster["status_extracao"] = "CONTEUDO_BLOQUEADO"
    cluster["texto_completo"] = ""
    cluster["motivo_bloqueio"] = ultimo_motivo
    cluster["necessita_extracao_manual"] = True
    cluster["historico_tentativas"] = historico
    return cluster


async def _navegar_playwright_item(context, item: dict) -> dict:
    """Navegação controlada via headless browser para resgate de matérias bloqueadas."""
    url_primaria = item.get("url_canonica_resolvida") or item.get("url_utilizada") or ""
    candidatas = [url_primaria] if url_primaria else []
    for esp in item.get("urls_espelho_disponiveis", []):
        if esp not in candidatas and esp.startswith("http"):
            candidatas.append(esp)

    titulo = item.get("titulo", "")
    page = None
    sucesso = False
    ultimo_motivo = "CONTEUDO_CURTO_OU_BLOQUEADO"

    try:
        page = await context.new_page()
        await page.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        async def interceptar_recursos(route):
            if route.request.resource_type in ["image", "media", "font", "stylesheet"]:
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", interceptar_recursos)

        for url_alvo in candidatas:
            cat_term = classificar_url_terminal(url_alvo)
            if cat_term:
                ultimo_motivo = cat_term
                continue

            try:
                await page.goto(url_alvo, wait_until="domcontentloaded", timeout=9000)
            except Exception:
                ultimo_motivo = "TIMEOUT_BROWSER"
                continue

            if "consent.google" in page.url or "google.com" in page.url:
                for sel in ["button:has-text('Aceitar tudo')", "button:has-text('Concordo')", "button:has-text('Accept all')"]:
                    try:
                        btn = page.locator(sel).first
                        if await btn.is_visible(timeout=600):
                            await btn.click()
                            break
                    except Exception:
                        pass
                try:
                    await page.wait_for_url(lambda u: "google" not in u, timeout=3000)
                except Exception:
                    pass

            url_real = page.url
            if "google.com" in url_real or "consent.google" in url_real:
                ultimo_motivo = "NAO_REDIRECIONOU_GOOGLE"
                continue

            html_content = await page.content()
            text = trafilatura.extract(html_content, include_comments=False, include_tables=False, output_format="txt")
            if not text or len(text.strip()) < TAMANHO_MINIMO_TEXTO:
                soup = BeautifulSoup(html_content, "html.parser")
                for tag in soup(["script", "style", "nav", "header", "footer", "aside"]):
                    tag.decompose()
                paragraphs = [p.get_text().strip() for p in soup.find_all("p") if len(p.get_text().strip()) > 35]
                text = "\n\n".join(paragraphs)

            apto, motivo = validar_integridade_factual(titulo, text)
            if apto:
                item["texto_completo"] = text.strip()
                item["status_extracao"] = "SUCESSO"
                item["status_resolucao"] = "RESOLVIDO"
                item["url_utilizada"] = url_real
                item["url_canonica_resolvida"] = url_real
                item["motivo_bloqueio"] = None
                item["necessita_extracao_manual"] = False
                sucesso = True
                break
            else:
                ultimo_motivo = motivo

        if not sucesso:
            item["status_extracao"] = "CONTEUDO_BLOQUEADO"
            item["texto_completo"] = ""
            item["motivo_bloqueio"] = ultimo_motivo

    except Exception as e:
        item["status_extracao"] = "CONTEUDO_BLOQUEADO"
        item["motivo_bloqueio"] = f"ERRO_PLAYWRIGHT: {type(e).__name__}"
    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass
    return item


async def processar_bloqueados_playwright(itens_bloqueados: list) -> list:
    """Pool assíncrono para execução do Playwright em paralelo."""
    if not itens_bloqueados:
        return []
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled", "--disable-gpu"],
        )
        context = await browser.new_context(user_agent=random.choice(USER_AGENTS), viewport={"width": 1280, "height": 800})
        sem = asyncio.Semaphore(8)

        async def _safe_run(item):
            async with sem:
                try:
                    return await asyncio.wait_for(_navegar_playwright_item(context, item), timeout=18.0)
                except asyncio.TimeoutError:
                    item["status_extracao"] = "CONTEUDO_BLOQUEADO"
                    item["motivo_bloqueio"] = "DEADLOCK_TIMEOUT"
                    return item

        resultados = await asyncio.gather(*[_safe_run(item) for item in itens_bloqueados])
        await context.close()
        await browser.close()
        return resultados


def executar_fallback_playwright(itens_bloqueados: list) -> list:
    """Interface síncrona padronizada para acionamento do Playwright."""
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        return loop.run_until_complete(processar_bloqueados_playwright(itens_bloqueados))
    finally:
        loop.close()
