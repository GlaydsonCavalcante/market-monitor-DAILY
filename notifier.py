"""
notifier.py

Módulo de despacho consolidado via Telegram para a esteira diária:
- Sanitização de tokens e identificadores de destino.
- Envio de sumário executivo em HTML com carimbo temporal no fuso de Brasília.
- Despacho do arquivo JSON consolidado como anexo estruturado multipart.
- Validação estrita de códigos HTTP sem tolerância a erros mascarados.
"""

from datetime import datetime
import os
import re
from zoneinfo import ZoneInfo
import requests

FUSO_BRASILIA = ZoneInfo("America/Sao_Paulo")


def enviar_telegram(
    regiao_nome: str,
    arquivos: list,
    total_brutas: int,
    total_clusters: int,
    total_processadas: int,
    sucessos: int,
    bloqueados: int,
) -> None:
    """
    Despacha resumo e arquivos JSON gerados pelo runner para os chats Telegram configurados.
    Interrompe a execução imediatamente (raise) caso as credenciais estejam ausentes
    ou a API recuse a entrega (status HTTP != 200).
    """
    raw_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip().strip('"').strip("'")
    chat_ids_raw = os.getenv("TELEGRAM_CHAT_ID", "").strip().strip('"').strip("'")

    if not raw_token:
        raise ValueError("A variável de ambiente TELEGRAM_BOT_TOKEN não foi definida.")
    if not chat_ids_raw:
        raise ValueError("A variável de ambiente TELEGRAM_CHAT_ID não foi definida.")

    # Remove eventual prefixo 'bot' acidental do token
    bot_token = raw_token[3:] if raw_token.lower().startswith("bot") else raw_token

    # Validação estrutural do token (<id_numerico>:<hash>) para falha rápida preventiva
    if not re.match(r"^\d{8,11}:[A-Za-z0-9_-]{30,}$", bot_token):
        raise ValueError(
            f"Formato inválido em TELEGRAM_BOT_TOKEN. "
            f"Comprimento: {len(bot_token)} caracteres. Esperado padrão '<id>:<hash>' do @BotFather."
        )

    chat_ids = [c.strip() for c in chat_ids_raw.split(",") if c.strip()]
    url_msg = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    url_doc = f"https://api.telegram.org/bot{bot_token}/sendDocument"

    taxa_sucesso = (sucessos / total_processadas * 100) if total_processadas > 0 else 0.0
    agora_bsb = datetime.now(FUSO_BRASILIA).strftime("%d/%m/%Y %H:%M:%S")

    resumo_msg = (
        f"📊 <b>MONITORAMENTO DIÁRIO: {regiao_nome.upper()}</b>\n\n"
        f"📅 <b>Data/Hora (BSB):</b> {agora_bsb}\n"
        f"⏱ <b>Janela:</b> Últimas 24 horas\n"
        f"📥 <b>Matérias Brutas:</b> {total_brutas}\n"
        f"🧩 <b>Clusters Únicos:</b> {total_clusters}\n"
        f"⚡ <b>Textos Extraídos:</b> {sucessos} ({taxa_sucesso:.1f}%)\n"
        f"🔒 <b>Bloqueadas / Falhas:</b> {bloqueados}\n\n"
        f"📎 <i>Arquivo JSON consolidado em anexo.</i>"
    )

    for chat_id in chat_ids:
        # 1. Envio do relatório executivo em texto HTML
        resp_msg = requests.post(
            url_msg,
            json={"chat_id": chat_id, "text": resumo_msg, "parse_mode": "HTML"},
            timeout=30,
        )
        if resp_msg.status_code != 200:
            raise RuntimeError(
                f"Falha ao enviar mensagem Telegram para {chat_id} (HTTP {resp_msg.status_code}): {resp_msg.text}"
            )

        # 2. Envio dos arquivos JSON anexados
        for caminho in arquivos:
            if not os.path.exists(caminho):
                continue
            nome_arquivo = os.path.basename(caminho)
            with open(caminho, "rb") as doc:
                resp_doc = requests.post(
                    url_doc,
                    data={"chat_id": chat_id},
                    files={"document": (nome_arquivo, doc, "application/json")},
                    timeout=60,
                )
                if resp_doc.status_code != 200:
                    raise RuntimeError(
                        f"Falha ao enviar documento {nome_arquivo} para {chat_id} (HTTP {resp_doc.status_code}): {resp_doc.text}"
                    )
