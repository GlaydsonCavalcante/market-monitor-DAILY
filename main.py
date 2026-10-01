"""
main.py

Orquestrador do Monitoramento de Mercado Diário:
- Varre estritamente as últimas 24 horas (when:1d).
- Não altera arquivos históricos no Google Drive.
- Agrupa matérias e extrai o texto integral com validação factual.
- Gera relatório no Step Summary e envia o arquivo consolidado via Telegram.
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
import importlib.util
import json
import random
import subprocess
import types

from notifier import enviar_telegram
import processor


def carregar_modulo_config(caminho_ou_shard: str):
    """Carrega dinamicamente a configuração do shard via streaming do Rclone ou local."""
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
        raise ValueError(f"Não foi possível decodificar {nome_arquivo}.")

    modulo = types.ModuleType("config_modulo")
    modulo.__file__ = f"<gdrive_config:{nome_arquivo}>"
    sys.modules["config_modulo"] = modulo
    exec(conteudo, modulo.__dict__)
    return modulo


def registrar_step_summary(regiao: str, total_brutas: int, total_clusters: int, sucessos: int, bloqueados: int) -> None:
    """Publica métricas na tela de Sumário do GitHub Actions."""
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


def executar_pipeline_diario(caminho_ou_shard: str) -> None:
    cfg = carregar_modulo_config(caminho_ou_shard)
    regiao = getattr(cfg, "REGIAO_NOME", "GLOBAL").lower()
    agora_bsb = datetime.now(FUSO_BRASILIA)
    ts = agora_bsb.strftime("%Y%m%d_%H%M%S")
    nome_json = f"noticias_{regiao}_{ts}.json"

    print(f"\n=======================================================", flush=True)
    print(f"MONITORAMENTO DIÁRIO: [{regiao.upper()}] | JANELA: Últimas 24 horas", flush=True)
    print(f"Horário de Início (BSB): {agora_bsb.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    print(f"=======================================================\n", flush=True)

    mercados = cfg.MERCADOS_ALVO
    timeout_req = getattr(cfg, "TIMEOUT_REQUISICAO", 6)
    max_workers = getattr(cfg, "MAX_WORKERS_PARALELO", 8)
    dominios = getattr(cfg, "DOMINIOS_PREFERENCIAIS", [])

    raw_articles = []

    # 1. Varredura RSS das últimas 24 horas
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

    print(f"\nTotal bruto coletado: {len(raw_articles)} matérias.", flush=True)

    if not raw_articles:
        print("Nenhuma matéria publicada nas últimas 24 horas.", flush=True)
        with open(nome_json, "w", encoding="utf-8") as f:
            json.dump([], f, ensure_ascii=False, indent=2)
        registrar_step_summary(regiao, 0, 0, 0, 0)
        return

    # 2. Agrupamento Semântico Rápido
    prefixo = f"CLUS_{agora_bsb.strftime('%Y%m%d')}_{regiao[:4].upper()}"
    clusters = processor.cluster_articles_rapido(raw_articles, similarity_threshold=80.0, prefixo_id=prefixo)
    print(f"Clusters consolidados: {len(clusters)}", flush=True)

    # 3. Extração Concorrente - Estágio 1: Fast HTTP
    def worker(c):
        time.sleep(random.uniform(0.05, 0.2))
        return processor.process_cluster_with_fallback(c, timeout=timeout_req)

    processed_results = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
        futuros = {executor.submit(worker, c): c for c in clusters}
        for f in concurrent.futures.as_completed(futuros):
            processed_results.append(f.result())

    # 4. Extração Concorrente - Estágio 2: Playwright Stealth para retidos
    bloqueados_indices = [i for i, r in enumerate(processed_results) if r["status_extracao"] != "SUCESSO"]
    if bloqueados_indices:
        print(f">> Ativando Playwright Stealth para {len(bloqueados_indices)} matérias bloqueadas...", flush=True)
        itens_pw = [processed_results[i] for i in bloqueados_indices]
        recuperados = processor.executar_fallback_playwright(itens_pw)
        for pos, idx_orig in enumerate(bloqueados_indices):
            processed_results[idx_orig] = recuperados[pos]

    sucessos = sum(1 for r in processed_results if r["status_extracao"] == "SUCESSO")
    bloqueados = len(processed_results) - sucessos
    taxa_sucesso = (sucessos / len(processed_results) * 100) if processed_results else 0.0

    print("\n" + "=" * 60, flush=True)
    print(f"CONSOLIDAÇÃO DIÁRIA: [{regiao.upper()}]", flush=True)
    print(f"Matérias Brutas : {len(raw_articles)}", flush=True)
    print(f"Clusters        : {len(clusters)}", flush=True)
    print(f"Sucessos        : {sucessos} ({taxa_sucesso:.1f}%)", flush=True)
    print(f"Bloqueios       : {bloqueados}", flush=True)
    print("=" * 60 + "\n", flush=True)

    # 5. Gravação Local
    with open(nome_json, "w", encoding="utf-8") as f:
        json.dump(processed_results, f, ensure_ascii=False, indent=2)

    # 6. Publicação de Telemetria e Despacho via Telegram
    registrar_step_summary(
        regiao=regiao,
        total_brutas=len(raw_articles),
        total_clusters=len(clusters),
        sucessos=sucessos,
        bloqueados=bloqueados,
    )

    enviar_telegram(
        regiao_nome=regiao,
        arquivos=[nome_json],
        total_brutas=len(raw_articles),
        total_clusters=len(clusters),
        total_processadas=len(processed_results),
        sucessos=sucessos,
        bloqueados=bloqueados,
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Monitoramento Diário de Mercado")
    parser.add_argument("--config", type=str, default=None)
    parser.add_argument("--shard", type=str, default=None)
    args = parser.parse_args()

    alvo = args.shard if args.shard else args.config
    if not alvo:
        raise ValueError("É obrigatório informar --shard ou --config.")

    executar_pipeline_diario(alvo)
