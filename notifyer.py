"""Módulo de mensageria e auditoria via Telegram para esteiras distribuídas."""

from datetime import datetime
import html
import logging
import os
from typing import Any, Dict, List
import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")


def obter_lista_chat_ids(raw_chat_ids: str | None) -> List[str]:
    """Extrai e higieniza lista de IDs de chat a partir de string separada por vírgulas."""
    if not raw_chat_ids:
        return []
    return [c.strip() for c in raw_chat_ids.split(",") if c.strip()]


def _despachar_mensagem_telegram(bot_token: str, chat_id: str, texto_html: str) -> None:
    """Envia requisição à API do Telegram e valida se foi aceita sem mascarar erros."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": texto_html,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, json=payload, timeout=20)
        if resp.status_code != 200:
            logging.error(f"[TELEGRAM ERRO {resp.status_code}] Chat {chat_id}: {resp.text}")
        else:
            logging.info(f"[TELEGRAM OK] Mensagem entregue ao Chat {chat_id}.")
    except Exception as exc:
        logging.error(f"[TELEGRAM FALHA REDE] Chat {chat_id}: {exc}")


def enviar_alerta_telegram(
    nome_arquivo: str,
    runner_id: int,
    total_itens: int,
    sucessos: int,
    desistencias: int,
    falhas_acesso: int,
    tempo_execucao_s: float,
    meta_minima: float = 42.0,
    meta_desejavel: float = 70.0,
) -> None:
    """Envia alerta individual após a conclusão do processamento de um lote."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_ids = obter_lista_chat_ids(os.getenv("TELEGRAM_CHAT_ID"))

    if not bot_token or not chat_ids:
        logging.error("[TELEGRAM CONFIG] TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID não encontrados no ambiente.")
        return

    universo_util = max(1, total_itens - desistencias)
    taxa_eficacia = (sucessos / universo_util) * 100.0
    minutos = int(tempo_execucao_s // 60)
    segundos = int(tempo_execucao_s % 60)

    if taxa_eficacia >= meta_desejavel:
        status_meta = f"META DESEJÁVEL ATINGIDA ({taxa_eficacia:.1f}% &gt;= {meta_desejavel:.0f}%)"
    elif taxa_eficacia >= meta_minima:
        status_meta = f"META MÍNIMA ATINGIDA ({taxa_eficacia:.1f}% &gt;= {meta_minima:.0f}%)"
    else:
        status_meta = f"ABAIXO DA META ({taxa_eficacia:.1f}% &lt; {meta_minima:.0f}%)"

    msg = (
        f"<b>LOTE CONCLUÍDO | RUNNER {runner_id}</b>\n\n"
        f"<b>Arquivo:</b> <code>{html.escape(nome_arquivo)}</code>\n"
        f"<b>Data/Hora:</b> {datetime.now().strftime('%d/%m/%Y %H:%M:%S')}\n"
        f"<b>Total no Lote:</b> {total_itens}\n"
        f"<b>Textos Extraídos:</b> {sucessos} ({taxa_eficacia:.1f}% viáveis)\n"
        f"<b>Desistências Estruturais:</b> {desistencias}\n"
        f"<b>Falhas de Acesso / Timeout:</b> {falhas_acesso}\n"
        f"<b>Duração:</b> {minutos}m {segundos}s\n"
        f"<b>Status:</b> {status_meta}"
    )

    for chat_id in chat_ids:
        _despachar_mensagem_telegram(bot_token, chat_id, msg)


def enviar_resumo_runner_telegram(
    runner_id: int,
    total_alocados: int,
    tempo_total_s: float,
    lista_detalhada: List[Dict[str, Any]],
) -> None:
    """Envia relatório consolidado de fechamento de partição com limite de tamanho estrito."""
    bot_token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_ids = obter_lista_chat_ids(os.getenv("TELEGRAM_CHAT_ID"))

    if not bot_token or not chat_ids:
        logging.error("[TELEGRAM CONFIG] TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID não encontrados no ambiente.")
        return

    minutos = int(tempo_total_s // 60)
    segundos = int(tempo_total_s % 60)

    sucessos = sum(1 for a in lista_detalhada if a.get("status") == "PROCESSADO")
    saltados = sum(1 for a in lista_detalhada if a.get("status") == "SALTADO")
    falhas = sum(1 for a in lista_detalhada if a.get("status") == "FALHA")

    linhas_arquivos = []
    for a in lista_detalhada:
        nome = html.escape(str(a.get("nome", "")))
        eficacia = float(a.get("eficacia", 0.0))
        st = a.get("status")
        if st == "PROCESSADO":
            linhas_arquivos.append(f"• <code>{nome}</code> ({eficacia:.1f}%)")
        elif st == "SALTADO":
            linhas_arquivos.append(f"• [SALTADO] <code>{nome}</code> ({eficacia:.1f}%)")
        else:
            linhas_arquivos.append(f"• [FALHA] <code>{nome}</code>")

    # Limita em 25 linhas para não estourar o limite de 4096 caracteres do Telegram
    detalhamento = "\n".join(linhas_arquivos[:25])
    if len(linhas_arquivos) > 25:
        detalhamento += f"\n• ... e mais {len(linhas_arquivos) - 25} arquivos."

    msg = (
        f"<b>RUNNER {runner_id} | CONCLUSAO DO BLOCO</b>\n"
        f"<b>Duração Total:</b> {minutos}m {segundos}s | <b>Arquivos Alocados:</b> {total_alocados}\n\n"
        f"<b>Consolidação:</b>\n"
        f"• Processados nesta rodada: {sucessos}\n"
        f"• Saltados (já na meta): {saltados}\n"
        f"• Falhas: {falhas}\n\n"
        f"<b>Detalhamento:</b>\n"
        f"{detalhamento}"
    )

    for chat_id in chat_ids:
        _despachar_mensagem_telegram(bot_token, chat_id, msg)
