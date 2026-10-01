"""Módulo de extração massiva de notícias em lote com resolução canônica e bypass stealth."""

import argparse
import asyncio
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import gc
import json
import logging
import os
import random
import re
import socket
import time
from typing import Dict, List, Optional, Tuple
from urllib.parse import urlparse

from bs4 import BeautifulSoup
import googlenewsdecoder as gnd
from playwright.async_api import async_playwright
import requests
from requests.adapters import HTTPAdapter
import trafilatura
from urllib3.util import Retry

from notifyer import enviar_alerta_telegram, enviar_resumo_runner_telegram

socket.setdefaulttimeout(3.0)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
)

# Constantes e Metas Metodológicas
META_SUCESSO_GLOBAL = 70.0
TAMANHO_MINIMO_TEXTO = 200
MAX_WORKERS_HTTP = 8

DOMINIOS_MIDIA = {
    "youtube.com",
    "youtu.be",
    "spotify.com",
    "open.spotify.com",
    "soundcloud.com",
    "vimeo.com",
    "globoplay.globo.com",
    "podcasts.apple.com",
}
DOMINIOS_FECHADOS = {
    "twitter.com",
    "x.com",
    "facebook.com",
    "instagram.com",
    "linkedin.com",
}

USER_AGENTS = [
    (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML,"
        " like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
        " (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36"
    ),
    (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko)"
        " Chrome/122.0.0.0 Safari/537.36"
    ),
]

# ==============================================================================
# PADRÕES DE BLOQUEIO DE INFRAESTRUTURA E VALIDAÇÃO FACTUAL
# ==============================================================================
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
]
REGEX_ANTIBOT = re.compile("|".join(PADROES_ANTIBOT), re.IGNORECASE)

STOPWORDS_TITULO = {
    "the", "a", "an", "and", "or", "but", "in", "on", "at", "to", "for", "of", "with",
    "by", "from", "up", "about", "into", "over", "after", "o", "a", "os", "as", "um",
    "uma", "de", "da", "do", "em", "para", "com", "por", "sobre", "el", "la", "los",
    "las", "en", "por", "para", "con", "del", "al", "der", "die", "das", "und", "im",
    "le", "les", "des", "pour", "dans", "sur", "que", "is", "are", "was", "were"
}


def validar_integridade_factual(titulo: str, texto: Optional[str]) -> Tuple[bool, str]:
    """
    Valida a integridade do conteúdo textual extraído antes de aceitar como SUCESSO.
    Rejeita textos curtos, assinaturas de infraestrutura CDN/WAF e ausência de sobreposição léxica.
    """
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
        if not any(token in texto_lower for token in tokens_titulo):
            return False, "SEM_SOBREPOSICAO_TITULO_CORPO"

    return True, "APTO"

def criar_sessao_http() -> requests.Session:
    """Configura pool persistente de conexões HTTP com retentativas limitadas."""
    s = requests.Session()
    retry = Retry(
        total=1,
        backoff_factor=0.2,
        status_forcelist=[429, 500, 502, 503, 504],
        raise_on_status=False,
    )
    adapter = HTTPAdapter(
        pool_connections=24, pool_maxsize=24, max_retries=retry
    )
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


HTTP_SESSION = criar_sessao_http()


def classificar_url_terminal(url: str) -> Optional[str]:
    """Identifica URLs que pertencem a redes fechadas, plataformas de mídia ou homepages."""
    if not url or not isinstance(url, str):
        return None
    try:
        parsed = urlparse(url.lower())
        netloc = parsed.netloc.replace("www.", "")
        if any(netloc == d or netloc.endswith("." + d) for d in DOMINIOS_MIDIA):
            return "CONTEUDO_MIDIA"
        if any(netloc == d or netloc.endswith("." + d) for d in DOMINIOS_FECHADOS):
            return "PLATAFORMA_FECHADA"
        path = parsed.path.strip("/")
        if not path or path in [
            "index.html",
            "index.php",
            "home",
            "noticias",
            "economia",
            "politica",
        ]:
            if not parsed.query:
                return "REDIRECT_HOMEPAGE"
    except Exception:
        pass
    return None


def sanitizar_url_canonica(url: Optional[str]) -> Optional[str]:
    """Valida se a URL é externa ao Google News e possui protocolo HTTP/HTTPS."""
    if not url or not isinstance(url, str):
        return None
    u = url.strip()
    if not u.startswith("http"):
        return None
    if any(
        g in u
        for g in [
            "news.google.com",
            "google.com",
            "googlenews",
            "consent.google",
        ]
    ):
        return None
    return u


def eh_canonica(url: str) -> bool:
    """Verifica se a URL já é canônica externa."""
    return sanitizar_url_canonica(url) is not None


def decodificar_offline(url_google: str) -> Optional[str]:
    """Decodifica URLs do Google News via Base64 offline quando estruturadas localmente."""
    if not url_google or "articles/" not in url_google:
        return None
    try:
        token = url_google.split("articles/")[-1].split("?")[0].strip()
        padded = token + "=" * (-len(token) % 4)
        raw = base64.urlsafe_b64decode(padded)
        for u in re.findall(rb"https?://[a-zA-Z0-9_\-\.\/\?\=\&\%\#\:\@]+", raw):
            u_str = u.decode("utf-8", errors="ignore").rstrip('\\"')
            if "google.com" not in u_str and "." in u_str and len(u_str) > 15:
                return u_str
    except Exception:
        pass
    return None


def extrair_canonica_html(html: str) -> Optional[str]:
    """Varre o início do HTML buscando tags canônicas ou tags de OpenGraph."""
    if not html:
        return None
    try:
        soup = BeautifulSoup(html[:20480], "html.parser")
        tag_link = soup.find(
            "link", rel=lambda val: val and "canonical" in val.lower()
        )
        if tag_link and tag_link.get("href"):
            u = sanitizar_url_canonica(tag_link["href"])
            if u:
                return u
        tag_og = soup.find("meta", property="og:url")
        if tag_og and tag_og.get("content"):
            u = sanitizar_url_canonica(tag_og["content"])
            if u:
                return u
    except Exception:
        pass
    return None


def decodificar_google_rpc(url: str) -> Optional[str]:
    """Resolve tokens criptografados do Google News diretamente via RPC da biblioteca interna."""
    if "news.google.com" not in url:
        return sanitizar_url_canonica(url)
    try:
        res = gnd.decoderv1(url, interval=0.1)
        if res and res.get("status"):
            return sanitizar_url_canonica(res.get("decoded_url"))
    except Exception:
        pass
    return None


def extrair_texto_hibrido(html_ou_texto: str) -> Optional[str]:
    """Extração de conteúdo textual jornalístico via Trafilatura com fallback estrutural."""
    if not html_ou_texto:
        return None

    texto = trafilatura.extract(
        html_ou_texto, include_comments=False, include_tables=False
    )
    if (
        texto
        and len(texto.strip()) >= TAMANHO_MINIMO_TEXTO
        and not REGEX_ANTIBOT.search(texto)
    ):
        return texto.strip()

    try:
        soup = BeautifulSoup(html_ou_texto, "html.parser")
        for elemento in soup(
            ["script", "style", "nav", "header", "footer", "aside", "form"]
        ):
            elemento.decompose()
        paragrafos = [
            p.get_text().strip()
            for p in soup.find_all("p")
            if len(p.get_text().strip()) >= 35
        ]
        texto_dom = "\n\n".join(paragrafos)
        if (
            len(texto_dom) >= TAMANHO_MINIMO_TEXTO
            and not REGEX_ANTIBOT.search(texto_dom)
        ):
            return texto_dom
    except Exception:
        pass

    return None


def extrair_texto_e_canonica_http(
    url: str,
) -> Tuple[Optional[str], Optional[str]]:
    """Executa requisição HTTP rápida retornando texto e metatag canônica se presentes."""
    try:
        headers = {"User-Agent": random.choice(USER_AGENTS)}
        resp = HTTP_SESSION.get(
            url, headers=headers, timeout=(2.5, 4.0), allow_redirects=True
        )
        if resp.status_code == 200 and resp.text:
            can_meta = extrair_canonica_html(resp.text)
            can_final = can_meta or sanitizar_url_canonica(resp.url)
            txt = extrair_texto_hibrido(resp.text)
            return txt, can_final
    except Exception:
        pass
    return None, None


def processar_item_estagio_http(item_tuple: Tuple[int, dict]) -> Tuple[int, dict, bool]:
    """Processamento rápido do Estágio 1: decodificação RPC e conexões HTTP diretas."""
    idx, item = item_tuple
    ja_tinha_txt = bool(
        item.get("status_extracao") == "SUCESSO" and item.get("texto_completo")
    )
    if ja_tinha_txt:
        return idx, item, True

    if item.get("status_extracao") == "FALHA_ACESSO":
        return idx, item, False

    url_p = (
        sanitizar_url_canonica(item.get("url_canonica_resolvida"))
        or item.get("url_utilizada")
        or item.get("url_original_rss")
        or ""
    )

    if not eh_canonica(url_p):
        u_off = sanitizar_url_canonica(decodificar_offline(url_p))
        if u_off:
            url_p = u_off
            item["url_canonica_resolvida"] = u_off
            item["url_utilizada"] = u_off

    if not eh_canonica(url_p) and "news.google.com" in url_p:
        u_rpc = decodificar_google_rpc(url_p)
        if u_rpc:
            url_p = u_rpc
            item["url_canonica_resolvida"] = u_rpc
            item["url_utilizada"] = u_rpc

    espelhos = (
        item.get("urls_espelho_canonicas")
        or item.get("urls_espelho_disponiveis")
        or item.get("urls_espelho")
        or []
    )
    espelhos_resolvidos = []
    for u_esp in espelhos:
        u_clean = sanitizar_url_canonica(u_esp)
        if u_clean:
            espelhos_resolvidos.append(u_clean)
        else:
            u_rpc_esp = decodificar_google_rpc(u_esp)
            espelhos_resolvidos.append(u_rpc_esp if u_rpc_esp else u_esp)

    if not eh_canonica(url_p):
        for u_e in espelhos_resolvidos:
            if eh_canonica(u_e):
                url_p = u_e
                item["url_canonica_resolvida"] = u_e
                item["url_utilizada"] = u_e
                break

    item["urls_espelho_canonicas"] = [
        u for u in espelhos_resolvidos if eh_canonica(u)
    ]
    item["urls_espelho_disponiveis"] = espelhos_resolvidos

    cat_term = classificar_url_terminal(url_p)
    if cat_term:
        item["status_resolucao"] = cat_term
        item["status_extracao"] = "FALHA_ESTRUTURAL"
        item["motivo_bloqueio"] = cat_term
        return idx, item, False

    # Construção da lista ordenada de URLs candidatas (Primária + Espelhos)
    candidatas = []
    if eh_canonica(url_p):
        candidatas.append(url_p)
    for u_esp in item["urls_espelho_canonicas"]:
        if u_esp not in candidatas and eh_canonica(u_esp):
            candidatas.append(u_esp)

    titulo_item = item.get("titulo", "")
    ultimo_motivo = "HTTP_TIMEOUT_OU_BLOQUEIO"

    # Itera sobre os espelhos até encontrar um texto que passe na validação factual
    for u_cand in candidatas:
        texto_cand, can_extra = extrair_texto_e_canonica_http(u_cand)
        if texto_cand:
            apto, motivo = validar_integridade_factual(titulo_item, texto_cand)
            if apto:
                item["url_canonica_resolvida"] = can_extra or u_cand
                item["url_utilizada"] = can_extra or u_cand
                item["texto_completo"] = texto_cand
                item["status_extracao"] = "SUCESSO"
                item["status_resolucao"] = "RESOLVIDO"
                item["motivo_bloqueio"] = None
                item["necessita_extracao_manual"] = False
                return idx, item, True
            else:
                ultimo_motivo = motivo

    if candidatas:
        item["status_extracao"] = "FALHA_ACESSO"
        item["motivo_bloqueio"] = ultimo_motivo
        item["status_resolucao"] = "RESOLVIDO"
        return idx, item, False

    item["status_resolucao"] = "PENDENTE"
    item["status_extracao"] = "PENDENTE"
    return idx, item, False


async def _navegar_com_timeout_estrito(
    context, item_tuple: Tuple[int, dict]
) -> Tuple[int, dict, bool]:
    """
    Execução controlada no navegador headless para contornar consent walls e renderizações dinâmicas.
    Aplica validação factual ativa e fallback imediato para URLs espelho do cluster.
    """
    idx, item = item_tuple
    titulo = item.get("titulo", "")
    url_primaria = (
        sanitizar_url_canonica(item.get("url_canonica_resolvida"))
        or item.get("url_utilizada")
        or item.get("url_original_rss")
        or ""
    )

    # Constrói fila ordenada de URLs candidatas (Primária + até 2 espelhos canônicos)
    candidatas = [url_primaria] if url_primaria else []
    for esp in item.get("urls_espelho_canonicas", []):
        if esp not in candidatas and eh_canonica(esp):
            candidatas.append(esp)

    page = None
    sucesso = False
    ultimo_motivo = "CONTEUDO_CURTO_OU_BLOQUEADO"

    try:
        page = await context.new_page()
        await page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )
        # Bloqueia recursos pesados para acelerar a carga e poupar banda do runner
        await page.route(
            "**/*",
            lambda r: (
                r.abort()
                if r.request.resource_type in ["image", "media", "font", "stylesheet"]
                else r.continue_()
            ),
        )

        for url_alvo in candidatas[:2]:
            cat_term = classificar_url_terminal(url_alvo)
            if cat_term:
                item["status_resolucao"] = cat_term
                item["status_extracao"] = "FALHA_ESTRUTURAL"
                item["motivo_bloqueio"] = cat_term
                return idx, item, False

            try:
                await page.goto(url_alvo, wait_until="domcontentloaded", timeout=12000)
            except Exception:
                ultimo_motivo = "TIMEOUT_BROWSER"
                continue

            # Bypass de consentimento do Google News quando aplicável
            if "consent.google" in page.url or "google.com" in page.url:
                for sel in [
                    "button:has-text('Aceitar tudo')",
                    "button:has-text('Concordo')",
                    "button:has-text('Accept all')",
                ]:
                    try:
                        btn = page.locator(sel).first
                        if await btn.is_visible(timeout=800):
                            await btn.click()
                            break
                    except Exception:
                        pass
                try:
                    await page.wait_for_url(lambda u: "google" not in u, timeout=3000)
                except Exception:
                    pass

            conteudo_html = await page.content()
            url_final = extrair_canonica_html(conteudo_html) or sanitizar_url_canonica(page.url)

            if not url_final:
                cat_term_redirect = classificar_url_terminal(page.url)
                if cat_term_redirect:
                    item["status_resolucao"] = cat_term_redirect
                    item["status_extracao"] = "FALHA_ESTRUTURAL"
                    item["motivo_bloqueio"] = cat_term_redirect
                    return idx, item, False
                ultimo_motivo = "NAO_REDIRECIONOU_GOOGLE"
                continue

            texto = extrair_texto_hibrido(conteudo_html)
            apto, motivo = validar_integridade_factual(titulo, texto)

            if apto:
                item["url_canonica_resolvida"] = url_final
                item["url_utilizada"] = url_final
                item["texto_completo"] = texto
                item["status_extracao"] = "SUCESSO"
                item["status_resolucao"] = "RESOLVIDO"
                item["motivo_bloqueio"] = None
                item["necessita_extracao_manual"] = False
                sucesso = True
                break
            else:
                ultimo_motivo = motivo

        if not sucesso and item.get("status_extracao") != "FALHA_ESTRUTURAL":
            item["status_extracao"] = "FALHA_ACESSO"
            item["motivo_bloqueio"] = ultimo_motivo

    except Exception as exc:
        item["status_extracao"] = "TIMEOUT_BROWSER"
        item["motivo_bloqueio"] = str(type(exc).__name__)
    finally:
        if page:
            try:
                await page.close()
            except Exception:
                pass

    return idx, item, sucesso


async def processar_lote_playwright_async(
    itens: List[Tuple[int, dict]],
) -> List[Tuple[int, dict, bool]]:
    """Gerencia instâncias assíncronas do Chromium sob semáforo restrito de 4 tarefas concorrentes."""
    if not itens:
        return []
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-blink-features=AutomationControlled",
                "--disable-gpu",
            ],
        )
        context = await browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            viewport={"width": 1280, "height": 800},
        )
        sem = asyncio.Semaphore(4)

        async def _safe_run(par):
            async with sem:
                try:
                    return await asyncio.wait_for(
                        _navegar_com_timeout_estrito(context, par), timeout=16.0
                    )
                except asyncio.TimeoutError:
                    par[1]["status_extracao"] = "TIMEOUT_BROWSER"
                    par[1]["motivo_bloqueio"] = "DEADLOCK_TIMEOUT"
                    return par[0], par[1], False

        res = await asyncio.gather(*[_safe_run(item_par) for item_par in itens])
        await context.close()
        await browser.close()
        return res


def processar_lote_playwright(
    itens: List[Tuple[int, dict]],
) -> List[Tuple[int, dict, bool]]:
    """Interface síncrona para execução do pool assíncrono do Playwright."""
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(processar_lote_playwright_async(itens))


def expurgar_recursos_sistema():
    """Finaliza processos residuais de renderização do Chromium e recicla a sessão HTTP."""
    global HTTP_SESSION
    os.system("pkill -9 -f chrome || true")
    os.system("pkill -9 -f playwright || true")
    gc.collect()
    try:
        HTTP_SESSION.close()
    except Exception:
        pass
    HTTP_SESSION = criar_sessao_http()


def processar_arquivo(caminho_arquivo: str, runner_id: int = 0) -> dict:
    """
    Executa a cadeia de extração completa para um único arquivo JSON.
    Estágio 1 (Fast HTTP) -> Estágio 2 (Playwright Stealth) -> Auditoria Factual Final -> Rclone Sync.
    """
    t_ini_arquivo = time.perf_counter()
    nome_arq = os.path.basename(caminho_arquivo)

    with open(caminho_arquivo, "r", encoding="utf-8") as f:
        lote = json.load(f)

    total_n = len(lote)
    if total_n == 0:
        return {"nome": nome_arq, "status": "VAZIO", "eficacia": 0.0}

    sucesso_ini = sum(
        1
        for it in lote
        if it.get("status_extracao") == "SUCESSO" and it.get("texto_completo")
    )
    desist_ini = sum(
        1
        for it in lote
        if it.get("status_resolucao")
        in [
            "CONTEUDO_MIDIA",
            "PLATAFORMA_FECHADA",
            "REDIRECT_HOMEPAGE",
            "LINK_MORTO",
        ]
    )
    univ_util = max(1, total_n - desist_ini)
    efic_ini = (sucesso_ini / univ_util * 100.0)

    if efic_ini >= META_SUCESSO_GLOBAL:
        logging.info(
            f"[SALTADO] {nome_arq} já alcançou a meta ({sucesso_ini}/{univ_util} = {efic_ini:.1f}%)"
        )
        return {"nome": nome_arq, "status": "SALTADO", "eficacia": efic_ini}

    logging.info(
        f"[PROCESSANDO] {nome_arq} - Total: {total_n} | Válidos Iniciais: {sucesso_ini} ({efic_ini:.1f}%)"
    )

    # --------------------------------------------------------------------------
    # Estágio 1: Fast HTTP (Pool concorrente de requests leves)
    # --------------------------------------------------------------------------
    pendentes_http = [
        (i, it)
        for i, it in enumerate(lote)
        if not (it.get("status_extracao") == "SUCESSO" and it.get("texto_completo"))
    ]
    random.shuffle(pendentes_http)

    CHUNK = 60
    for c in range(0, len(pendentes_http), CHUNK):
        chunk = pendentes_http[c : c + CHUNK]
        with ThreadPoolExecutor(max_workers=MAX_WORKERS_HTTP) as executor:
            futuros = {
                executor.submit(processar_item_estagio_http, par): par[0]
                for par in chunk
            }
            for fut in as_completed(futuros):
                idx, item_proc, _ = fut.result()
                lote[idx] = item_proc

        suc_atual = sum(
            1
            for it in lote
            if it.get("status_extracao") == "SUCESSO" and it.get("texto_completo")
        )
        if (suc_atual / univ_util * 100.0) >= META_SUCESSO_GLOBAL:
            break

    # --------------------------------------------------------------------------
    # Estágio 2: Headless Browser (Playwright com evasão stealth e fallback)
    # --------------------------------------------------------------------------
    suc_pos_http = sum(
        1
        for it in lote
        if it.get("status_extracao") == "SUCESSO" and it.get("texto_completo")
    )
    if (suc_pos_http / univ_util * 100.0) < META_SUCESSO_GLOBAL:
        pendentes_pw = [
            (i, it)
            for i, it in enumerate(lote)
            if it.get("status_extracao") in ["PENDENTE", "FALHA_ACESSO"]
            and it.get("status_resolucao")
            not in [
                "CONTEUDO_MIDIA",
                "PLATAFORMA_FECHADA",
                "REDIRECT_HOMEPAGE",
                "LINK_MORTO",
            ]
        ]
        random.shuffle(pendentes_pw)
        for c in range(0, len(pendentes_pw), 20):
            chunk = pendentes_pw[c : c + 20]
            res_chunk = processar_lote_playwright(chunk)
            for idx, item_proc, _ in res_chunk:
                lote[idx] = item_proc

            suc_atual = sum(
                1
                for it in lote
                if it.get("status_extracao") == "SUCESSO" and it.get("texto_completo")
            )
            if (suc_atual / univ_util * 100.0) >= META_SUCESSO_GLOBAL:
                break

    # --------------------------------------------------------------------------
    # Estágio 3: Harmonização Canônica e Validação Factual Rigorosa
    # --------------------------------------------------------------------------
    for item in lote:
        u_can = item.get("url_canonica_resolvida")
        u_util = item.get("url_utilizada")
        esps_disp = (
            item.get("urls_espelho_disponiveis") or item.get("urls_espelho") or []
        )

        if not sanitizar_url_canonica(u_can) and sanitizar_url_canonica(u_util):
            item["url_canonica_resolvida"] = u_util.strip()
            item["status_resolucao"] = "RESOLVIDO"
        elif not sanitizar_url_canonica(item.get("url_canonica_resolvida")):
            for e in esps_disp:
                if sanitizar_url_canonica(e):
                    item["url_canonica_resolvida"] = e.strip()
                    item["url_utilizada"] = e.strip()
                    item["status_resolucao"] = "RESOLVIDO"
                    break

        item["urls_espelho_canonicas"] = [
            e for e in esps_disp if sanitizar_url_canonica(e)
        ]

        txt = item.get("texto_completo")
        tit = item.get("titulo", "")
        # Checagem estrita de integridade factual antes de confirmar SUCESSO
        if txt and item.get("status_extracao") == "SUCESSO":
            apto, motivo = validar_integridade_factual(tit, txt)
            if not apto:
                item["texto_completo"] = None
                item["status_extracao"] = "FALHA_ACESSO"
                item["motivo_bloqueio"] = motivo
                item["necessita_extracao_manual"] = True

    # --------------------------------------------------------------------------
    # Gravação Atômica Local
    # --------------------------------------------------------------------------
    tmp = caminho_arquivo + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f_out:
        json.dump(lote, f_out, ensure_ascii=False, indent=2)
    os.replace(tmp, caminho_arquivo)

    # --------------------------------------------------------------------------
    # Métricas Finais e Notificações
    # --------------------------------------------------------------------------
    t_duracao = time.perf_counter() - t_ini_arquivo
    suc_final = sum(
        1
        for it in lote
        if it.get("status_extracao") == "SUCESSO" and it.get("texto_completo")
    )
    desist_final = sum(
        1
        for it in lote
        if it.get("status_resolucao")
        in [
            "CONTEUDO_MIDIA",
            "PLATAFORMA_FECHADA",
            "REDIRECT_HOMEPAGE",
            "LINK_MORTO",
        ]
    )
    bloq_final = sum(
        1
        for it in lote
        if it.get("status_extracao") in ["FALHA_ACESSO", "TIMEOUT_BROWSER"]
    )

    enviar_alerta_telegram(
        nome_arquivo=nome_arq,
        runner_id=runner_id,
        total_itens=total_n,
        sucessos=suc_final,
        desistencias=desist_final,
        falhas_acesso=bloq_final,
        tempo_execucao_s=t_duracao,
    )

    # Sincronização Imediata com Google Drive via Rclone
    remote_path = os.getenv("RCLONE_REMOTE_PATH", "gdrive_dados:")
    os.system(f"rclone copyto '{caminho_arquivo}' '{remote_path}{nome_arq}'")

    expurgar_recursos_sistema()
    efic_final = (suc_final / univ_util * 100.0)
    logging.info(
        f"[CONCLUÍDO] {nome_arq} processado ({suc_final}/{univ_util} = {efic_final:.1f}%) e sincronizado no Drive."
    )
    return {"nome": nome_arq, "status": "PROCESSADO", "eficacia": efic_final}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--arquivos", nargs="+", required=True, help="Arquivos para processar")
    parser.add_argument("--runner_id", type=int, default=0, help="Identificador do Runner")
    parser.add_argument("--meta_sucesso", type=float, default=70.0, help="Meta desejável de sucesso global")
    args = parser.parse_args()

    META_SUCESSO_GLOBAL = args.meta_sucesso
    t_inicio_bloco = time.perf_counter()
    auditoria_bloco = []

    for arq in args.arquivos:
        if os.path.exists(arq):
            try:
                res = processar_arquivo(arq, runner_id=args.runner_id)
                if res:
                    auditoria_bloco.append(res)
                else:
                    auditoria_bloco.append({
                        "nome": os.path.basename(arq),
                        "status": "SALTADO",
                        "eficacia": 0.0,
                    })
            except Exception as e:
                logging.error(f"Falha operacional em {arq}: {e}")
                auditoria_bloco.append({
                    "nome": os.path.basename(arq),
                    "status": "FALHA",
                    "eficacia": 0.0,
                })

    t_duracao_bloco = time.perf_counter() - t_inicio_bloco
    enviar_resumo_runner_telegram(
        runner_id=args.runner_id,
        total_alocados=len(args.arquivos),
        tempo_total_s=t_duracao_bloco,
        lista_detalhada=auditoria_bloco,
    )
