"""Módulo de descoberta retroativa de notícias RSS com particionamento temporal e temático."""

import argparse
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import glob
import importlib.util
import json
import logging
import os
import random
import re
import time
from typing import Any, Dict, List, Tuple
from urllib.parse import quote

import feedparser
import requests
from requests.adapters import HTTPAdapter
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity
from urllib3.util import Retry

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]


def criar_sessao() -> requests.Session:
    s = requests.Session()
    retry = Retry(total=2, backoff_factor=0.3, status_forcelist=[429, 500, 502, 503, 504], raise_on_status=False)
    adapter = HTTPAdapter(pool_connections=24, pool_maxsize=24, max_retries=retry)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


HTTP_SESSION = criar_sessao()


def sanitizar_nome(nome: str) -> str:
    s = re.sub(r"[^\w\-]", "_", nome.lower())
    return re.sub(r"_+", "_", s).strip("_")


def carregar_configs(diretorio_configs: str) -> List[Any]:
    arquivos = sorted(glob.glob(os.path.join(diretorio_configs, "config_*.py")))
    if not arquivos:
        raise FileNotFoundError(f"Nenhum arquivo config_*.py encontrado em: {diretorio_configs}")
    mods = []
    for caminho in arquivos:
        nome = os.path.splitext(os.path.basename(caminho))[0]
        spec = importlib.util.spec_from_file_location(nome, caminho)
        if spec and spec.loader:
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            mods.append(mod)
    return mods


def gerar_meses_decrescentes(meses_totais: int) -> List[Tuple[int, int, str, str]]:
    hoje = datetime.now()
    intervalos = []
    ano, mes = hoje.year, hoje.month
    for i in range(meses_totais):
        m = mes - i
        a = ano
        while m <= 0:
            m += 12
            a -= 1
        d_ini = f"{a}-{m:02d}-01"
        d_fim = f"{a+1}-01-01" if m == 12 else f"{a}-{m+1:02d}-01"
        intervalos.append((a, m, d_ini, d_fim))
    return sorted(intervalos, key=lambda x: (x[0], x[1]), reverse=True)


def pre_agrupar(materias: List[Dict], threshold: float = 0.78) -> List[Dict]:
    unicas = {}
    for m in materias:
        k = re.sub(r"\W+", "", m["titulo"].lower())
        if k not in unicas:
            unicas[k] = m
        else:
            unicas[k]["urls_espelho"].append(m["url_bruta"])
    cand = list(unicas.values())
    if len(cand) <= 1:
        return cand

    matriz = TfidfVectorizer(max_features=3500).fit_transform([m["titulo"] for m in cand])
    sim = cosine_similarity(matriz)
    visitados = set()
    res = []
    for i in range(len(cand)):
        if i in visitados:
            continue
        pai = cand[i]
        espelhos = set(pai.get("urls_espelho", []))
        for j in range(i + 1, len(cand)):
            if j not in visitados and sim[i, j] >= threshold:
                visitados.add(j)
                espelhos.add(cand[j]["url_bruta"])
        pai["urls_espelho"] = list(espelhos)
        res.append(pai)
        visitados.add(i)
    return res


def fetch_feed(param: Tuple) -> List[Dict]:
    q_str, d_ini, d_fim, hl, gl, lang, tema, regiao, excluidos = param
    time.sleep(random.uniform(0.02, 0.05))
    url = f"https://news.google.com/rss/search?q={quote(f'{q_str} after:{d_ini} before:{d_fim}')}&hl={hl}&gl={gl}&ceid={gl}:{hl}"
    itens = []
    try:
        resp = HTTP_SESSION.get(url, headers={"User-Agent": random.choice(USER_AGENTS)}, timeout=(2.5, 5.0))
        if resp.status_code == 200:
            feed = feedparser.parse(resp.content)
            for e in feed.entries:
                tit = getattr(e, "title", "").strip()
                if not tit or any(x.lower() in tit.lower() for x in excluidos):
                    continue
                itens.append({
                    "regiao_shard": regiao,
                    "tema": tema,
                    "termo_origem": q_str[:120],
                    "idioma": hl,
                    "lang_iso": lang,
                    "pais_emissao": gl,
                    "titulo": tit,
                    "data_noticia": getattr(e, "published", f"{d_ini}T00:00:00Z"),
                    "fonte_utilizada": getattr(e, "source", {}).get("title", "Fonte Externa"),
                    "url_bruta": e.link,
                    "urls_espelho": [],
                })
        else:
            logging.warning(f"[RSS HTTP {resp.status_code}] Falha na query '{q_str[:30]}...'")
    except Exception as exc:
        logging.warning(f"[RSS EXCEÇÃO] {exc} na query '{q_str[:30]}...'")
    return itens


def executar_coleta_historica(meses_totais: int, pasta_saida: str, pasta_configs: str) -> None:
    os.makedirs(pasta_saida, exist_ok=True)
    configs = carregar_configs(pasta_configs)
    intervalos = gerar_meses_decrescentes(meses_totais)
    logging.info(f"Iniciando descoberta histórica para os últimos {meses_totais} meses.")

    for ano, mes, d_ini, d_fim in intervalos:
        tag_mes = f"{ano}_{mes:02d}"
        data_cluster_tag = f"{ano}{mes:02d}01"
        tarefas = []

        for mod in configs:
            reg = getattr(mod, "REGIAO_NOME", "GLOBAL")
            mon = getattr(mod, "MONITORAMENTOS", {})
            alvo = getattr(mod, "MERCADOS_ALVO", [])
            exc = getattr(mod, "TERMOS_EXCLUIDOS", {})
            for m in alvo:
                gl, hl, lang = m["gl"], m["hl"], m["lang"]
                for tema, q_dict in mon.items():
                    for q in q_dict.get(lang, []):
                        tarefas.append((q, d_ini, d_fim, hl, gl, lang, tema, reg, exc.get(lang, [])))

        logging.info(f">> Processando Mês {tag_mes} | Total de queries: {len(tarefas)}")
        materias_por_tema = defaultdict(list)

        with ThreadPoolExecutor(max_workers=8) as executor:
            futuros = [executor.submit(fetch_feed, t) for t in tarefas]
            for fut in as_completed(futuros):
                res = fut.result()
                for item in res:
                    materias_por_tema[item["tema"]].append(item)

        total_bruto_mes = sum(len(v) for v in materias_por_tema.values())
        logging.info(f"Mês {tag_mes} coletou {total_bruto_mes} matérias brutas. Gerando shards...")

        for tema_nome, lista_bruta in materias_por_tema.items():
            tema_slug = sanitizar_nome(tema_nome)
            arq_shard = os.path.join(pasta_saida, f"noticias_raw_{tag_mes}_{tema_slug}.json")

            if os.path.exists(arq_shard):
                continue

            materias_unicas = pre_agrupar(lista_bruta, threshold=0.78)
            lote_salvar = []
            for i, item in enumerate(materias_unicas):
                lote_salvar.append({
                    "id_cluster": f"CLUS_{data_cluster_tag}_{tema_slug[:4].upper()}_{i+1:04d}",
                    "tema": item["tema"],
                    "termo_origem": item["termo_origem"],
                    "titulo": item["titulo"],
                    "data_noticia": item["data_noticia"],
                    "idioma": item["idioma"],
                    "fonte_utilizada": item["fonte_utilizada"],
                    "url_utilizada": item["url_bruta"],
                    "urls_espelho_disponiveis": item.get("urls_espelho", []),
                    "status_extracao": "PENDENTE",
                    "texto_completo": None,
                    "motivo_bloqueio": None,
                    "necessita_extracao_manual": True,
                    "historico_tentativas": [],
                })

            tmp = arq_shard + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f_out:
                json.dump(lote_salvar, f_out, ensure_ascii=False, indent=2)
            os.replace(tmp, arq_shard)

    logging.info(f"Descoberta histórica concluída. Shards gravados em {pasta_saida}.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Coletor Histórico de Notícias")
    parser.add_argument("--meses_totais", type=int, default=1, help="Quantidade de meses retroativos")
    parser.add_argument("--pasta_saida", type=str, default="dados_fila_local", help="Pasta de saída local")
    parser.add_argument("--pasta_configs", type=str, default="configs", help="Pasta com os arquivos config_*.py")
    args = parser.parse_args()

    executar_coleta_historica(args.meses_totais, args.pasta_saida, args.pasta_configs)
