"""
main.py

Orquestrador do Monitoramento de Mercado Diário:
- Suporta coleta isolada em matriz por shard (--coletar-apenas).
- Executa consolidacao translinguistica global com reducao semantica (--consolidar-global).
- Extrai texto integral restrito aos artigos canonicos dos clusters.
- Mantem o schema corporativo intacto com 'vetor_1024: null' para indexacao downstream via BGE-M3.
"""

import os
import sys
import time
from zoneinfo import ZoneInfo

os.environ["TZ"] = "America/Sao_Paulo"
if hasattr(time, "tzset"):
    time.tzset()

FUSO_BRASILIA = ZoneInfo("America/Sao_Paulo")

import argparse
import concurrent.futures
from datetime import datetime
import glob
import importlib.util
import json
import random
import subprocess
import types

from notifier import enviar_telegram
import processor


def carregar_modulo_config(caminho_ou_shard: str):
    """Carrega dinamicamente a configuracao do shard via streaming do Rclone ou local."""
    if os.path.exists(caminho_ou_shard):
        spec = importlib.util.spec_from_file_location("config_modulo", caminho_ou_shard)
        modulo = importlib.util.module_from_spec(spec)
        sys.modules["config_modulo"] = modulo
        spec.loader.exec_module(modulo)
        return modulo

    nome_arquivo = f"config_{caminho_ou_shard}.py" if not caminho_ou_shard.endswith(".py") else caminho_ou_shard
    comando = ["rclone", "cat", f"gdrive_config:{nome_arquivo}"]
    proc = subprocess.run(comando, capture_output=True)
    if proc.returncode != 0:
        erro_msg = proc.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"Falha ao ler {nome_arquivo} do Google Drive: {erro_msg}")

    conteudo = None
    for enc in ["utf-8-sig", "utf-8", "utf-16", "latin-1"]:
        try:
            conteudo = proc.stdout.decode(enc)
            break
        except UnicodeDecodeError:
            continue

    if conteudo is None:
        raise ValueError(f"Nao foi possivel decodificar {nome_arquivo}.")

    modulo = types.ModuleType("config_modulo")
    modulo.__file__ = f"<gdrive_config:{nome_arquivo}>"
    sys.modules["config_modulo"] = modulo
    exec(conteudo, modulo.__dict__)
    return modulo


def coletar_artigos_rss(cfg) -> list:
    """Varre feeds RSS do Google News para os termos e mercados do modulo de configuracao."""
    mercados = cfg.MERCADOS_ALVO
    timeout_req = getattr(cfg, "TIMEOUT_REQUISICAO", 6)
    dominios = getattr(cfg, "DOMINIOS_PREFERENCIAIS", [])
    raw_articles = []

    for tema, dict_idiomas in cfg.MONITORAMENTOS.items():
        print(f">> Coletando tema: {tema}...", flush=True)
        for mercado in mercados:
            gl, hl, lang = mercado["gl"], mercado["hl"], mercado["lang"]
            termos = dict_idiomas.get(lang, [])
            excluidos = getattr(cfg, "TERMOS_EXCLUIDOS", {}).get(lang, [])

            for termo in termos:
                query_base = processor.build_rss_query(termo, excluidos, dominios)
                query_final = f"{query_base} when:1d".strip()

                itens = processor.fetch_rss_feed(query_final, hl=hl, gl=gl, timeout=timeout_req)
                for item in itens:
                    item["tema"] = tema
                    item["termo_origem"] = termo
                    item["pais_emissao"] = gl
                    item["idioma"] = hl
                    raw_articles.append(item)

    return raw_articles


def registrar_step_summary(regiao: str, total_brutas: int, total_clusters: int, sucessos: int, bloqueados: int) -> None:
    """Publica metricas consolidadas na tela de Sumario do GitHub Actions."""
    caminho_summary = os.getenv("GITHUB_STEP_SUMMARY")
    if not caminho_summary:
        return

    taxa = (sucessos / (sucessos + bloqueados) * 100) if (sucessos + bloqueados) > 0 else 0.0
    agora = datetime.now(FUSO_BRASILIA).strftime("%d/%m/%Y %H:%M:%S")

    markdown = (
        f"### 📊 Monitoramento Diário: {regiao.upper()}\n\n"
        f"**Data/Hora Execução:** {agora} (Horário de Brasília)\n"
        f"**Janela de Coleta:** Últimas 24 horas (`when:1d`)\n\n"
        f"| Métrica | Valor |\n"
        f"| :--- | :--- |\n"
        f"| Matérias Brutas Coletadas | {total_brutas} |\n"
        f"| Clusters Consolidados | {total_clusters} |\n"
        f"| Textos Extraídos com Sucesso | {sucessos} |\n"
        f"| Bloqueadas / Falhas | {bloqueados} |\n"
        f"| Taxa de Eficácia | {taxa:.1f}% |\n\n"
        f"---\n"
    )

    with open(caminho_summary, "a", encoding="utf-8") as f:
        f.write(markdown)


def executar_coleta_shard(caminho_ou_shard: str) -> None:
    """Execucao estrita de coleta RSS para matriz paralela de shards."""
    cfg = carregar_modulo_config(caminho_ou_shard)
    shard_id = os.path.basename(caminho_ou_shard).replace("config_", "").replace(".py", "").strip().lower()

    print(f"Coletando RSS para Shard: [{shard_id.upper()}]", flush=True)
    artigos = coletar_artigos_rss(cfg)
    print(f"Total bruto coletado no shard: {len(artigos)} materias.", flush=True)

    arquivo_saida = f"raw_{shard_id}.json"
    with open(arquivo_saida, "w", encoding="utf-8") as f:
        json.dump(artigos, f, ensure_ascii=False, indent=2)


def executar_pipeline_global() -> None:
    """
    Consolidador Global:
    1. Unifica todos os arquivos raw_*.json produzidos pela matriz de shards.
    2. Agrupa os titulos globalmente via embeddings multilíngues ONNX.
    3. Executa a extracao de texto integral restrita aos clusters unificados.
    4. Gera arquivo JSON consolidado unico e emite despacho individualizado ao Telegram.
    """
    arquivos_raw = glob.glob("raw_*.json")
    if not arquivos_raw:
        raise FileNotFoundError("Nenhum arquivo 'raw_*.json' localizado para consolidacao global.")

    raw_articles = []
    for arq in arquivos_raw:
        with open(arq, "r", encoding="utf-8") as f:
            raw_articles.extend(json.load(f))

    agora_bsb = datetime.now(FUSO_BRASILIA)
    ts = agora_bsb.strftime("%Y%m%d_%H%M%S")
    nome_json = f"noticias_consolidado_{ts}.json"

    print(f"\n=======================================================", flush=True)
    print(f"CONSOLIDACAO GLOBAL DE MERCADO | JANELA: Ultimas 24 horas", flush=True)
    print(f"Horario de Inicio (BSB): {agora_bsb.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"Total bruto coletado (Cross-Shard): {len(raw_articles)} materias", flush=True)
    print(f"=======================================================\n", flush=True)

    if not raw_articles:
        with open(nome_json, "w", encoding="utf-8") as f:
            json.dump([], f, ensure_ascii=False, indent=2)
        registrar_step_summary("GLOBAL", 0, 0, 0, 0)
        enviar_telegram("GLOBAL", [nome_json], 0, 0, 0, 0, 0)
        return

    # Agrupamento Semantico Multilingue (Cross-lingual) via ONNX
    prefixo = f"CLUS_{agora_bsb.strftime('%Y%m%d')}_GLOB"
    clusters_totais = processor.cluster_articles_semantico(raw_articles, similarity_threshold=0.72, prefixo_id=prefixo)
    print(f"Clusters unificados globalmente (brutos): {len(clusters_totais)}", flush=True)

    # Filtragem e priorizacao de clusters para extracao
    clusters = selecionar_clusters_prioritarios(clusters_totais, max_unitarios_por_tema=80)
    print(f"Clusters selecionados para extracao de texto: {len(clusters)}", flush=True)

    # Extracao de Texto - Estagio 1: Fast HTTP Concorrente
    def worker(c):
        time.sleep(random.uniform(0.05, 0.2))
        return processor.process_cluster_with_fallback(c, timeout=6)

    processed_results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as executor:
        futuros = {executor.submit(worker, c): c for c in clusters}
        for f in concurrent.futures.as_completed(futuros):
            processed_results.append(f.result())

    # Checkpoint intermediario de seguranca
    with open(nome_json, "w", encoding="utf-8") as f:
        json.dump(processed_results, f, ensure_ascii=False, indent=2)

    # Extracao de Texto - Estagio 2: Playwright Stealth para retidos
    bloqueados_indices = [i for i, r in enumerate(processed_results) if r["status_extracao"] != "SUCESSO"]
    if bloqueados_indices:
        print(f">> Ativando Playwright Stealth para {len(bloqueados_indices)} materias bloqueadas...", flush=True)
        itens_pw = [processed_results[i] for i in bloqueados_indices]
        recuperados = processor.executar_fallback_playwright(itens_pw)
        for pos, idx_orig in enumerate(bloqueados_indices):
            processed_results[idx_orig] = recuperados[pos]

    sucessos = sum(1 for r in processed_results if r["status_extracao"] == "SUCESSO")
    bloqueados = len(processed_results) - sucessos
    taxa_sucesso = (sucessos / len(processed_results) * 100) if processed_results else 0.0

    print("\n" + "=" * 60, flush=True)
    print(f"CONSOLIDAÇÃO FINAL GLOBAL", flush=True)
    print(f"Matérias Brutas : {len(raw_articles)}", flush=True)
    print(f"Clusters        : {len(clusters)}", flush=True)
    print(f"Sucessos        : {sucessos} ({taxa_sucesso:.1f}%)", flush=True)
    print(f"Bloqueios       : {bloqueados}", flush=True)
    print("=" * 60 + "\n", flush=True)

    # Gravacao do JSON consolidado final
    with open(nome_json, "w", encoding="utf-8") as f:
        json.dump(processed_results, f, ensure_ascii=False, indent=2)

    registrar_step_summary(
        regiao="GLOBAL",
        total_brutas=len(raw_articles),
        total_clusters=len(clusters),
        sucessos=sucessos,
        bloqueados=bloqueados,
    )

    enviar_telegram(
        regiao_nome="GLOBAL",
        arquivos=[nome_json],
        total_brutas=len(raw_articles),
        total_clusters=len(clusters),
        total_processadas=len(processed_results),
        sucessos=sucessos,
        bloqueados=bloqueados,
    )


def executar_pipeline_diario(caminho_ou_shard: str) -> None:
    """Execucao autonoma de pipeline para um shard isolado (compatibilidade retroativa)."""
    cfg = carregar_modulo_config(caminho_ou_shard)
    shard_id = os.path.basename(caminho_ou_shard).replace("config_", "").replace(".py", "").strip().lower()
    regiao = getattr(cfg, "REGIAO_NOME", shard_id).lower()

    agora_bsb = datetime.now(FUSO_BRASILIA)
    ts = agora_bsb.strftime("%Y%m%d_%H%M%S")
    nome_json = f"noticias_{shard_id}_{ts}.json"

    raw_articles = coletar_artigos_rss(cfg)
    if not raw_articles:
        with open(nome_json, "w", encoding="utf-8") as f:
            json.dump([], f, ensure_ascii=False, indent=2)
        registrar_step_summary(regiao, 0, 0, 0, 0)
        enviar_telegram(regiao, [nome_json], 0, 0, 0, 0, 0)
        return

    prefixo = f"CLUS_{agora_bsb.strftime('%Y%m%d')}_{regiao[:4].upper()}"
    clusters = processor.cluster_articles_semantico(raw_articles, similarity_threshold=0.82, prefixo_id=prefixo)

    def worker(c):
        time.sleep(random.uniform(0.05, 0.2))
        return processor.process_cluster_with_fallback(c, timeout=getattr(cfg, "TIMEOUT_REQUISICAO", 6))

    processed_results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=getattr(cfg, "MAX_WORKERS_PARALELO", 8)) as executor:
        futuros = {executor.submit(worker, c): c for c in clusters}
        for f in concurrent.futures.as_completed(futuros):
            processed_results.append(f.result())

    with open(nome_json, "w", encoding="utf-8") as f:
        json.dump(processed_results, f, ensure_ascii=False, indent=2)

    bloqueados_indices = [i for i, r in enumerate(processed_results) if r["status_extracao"] != "SUCESSO"]
    if bloqueados_indices:
        itens_pw = [processed_results[i] for i in bloqueados_indices]
        recuperados = processor.executar_fallback_playwright(itens_pw)
        for pos, idx_orig in enumerate(bloqueados_indices):
            processed_results[idx_orig] = recuperados[pos]

    sucessos = sum(1 for r in processed_results if r["status_extracao"] == "SUCESSO")
    bloqueados = len(processed_results) - sucessos

    with open(nome_json, "w", encoding="utf-8") as f:
        json.dump(processed_results, f, ensure_ascii=False, indent=2)

    registrar_step_summary(regiao, len(raw_articles), len(clusters), sucessos, bloqueados)
    enviar_telegram(regiao, [nome_json], len(raw_articles), len(clusters), len(processed_results), sucessos, bloqueados)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Monitoramento Diário de Mercado")
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--shard", type=str, default=None)
    parser.add_argument("--coletar-apenas", action="store_true", help="Executa apenas a varredura RSS do shard")
    parser.add_argument("--consolidar-global", action="store_true", help="Consolida todos os shards coletados")
    args = parser.parse_args()

    if args.consolidar_global:
        executar_pipeline_global()
    else:
        alvo = args.shard if args.shard else args.config
        if not alvo:
            raise ValueError("É obrigatório informar --shard, --config ou acionar --consolidar-global.")
        if args.coletar_apenas:
            executar_coleta_shard(alvo)
        else:
            executar_pipeline_diario(alvo)

def selecionar_clusters_prioritarios(
    clusters: list,
    max_unitarios_por_tema: int = 80
) -> list:
    """
    Filtra clusters consolidados para raspagem e enriquecimento via LLM:
    1. Retém 100% dos clusters multi-fonte (2 ou mais matérias agregadas).
    2. Aplica cota máxima sobre clusters de fonte única por tema para mitigar
       dispersão temática em 'Tendências' e controlar custos de processamento.
    """
    clusters_multisource = []
    unitarios_por_tema = {}

    for c in clusters:
        tema = c.get("tema", "GERAL")
        total_fontes = 1 + len(c.get("urls_espelho", []))
        
        if total_fontes >= 2:
            clusters_multisource.append(c)
        else:
            if tema not in unitarios_por_tema:
                unitarios_por_tema[tema] = []
            if len(unitarios_por_tema[tema]) < max_unitarios_por_tema:
                unitarios_por_tema[tema].append(c)

    selecionados = list(clusters_multisource)
    for lista_unitarios in unitarios_por_tema.values():
        selecionados.extend(lista_unitarios)

    return selecionados
