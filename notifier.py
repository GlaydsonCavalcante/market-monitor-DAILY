"""
notifier.py

Módulo de despacho consolidado via Telegram:
- Envia resumo executivo em HTML com carimbo temporal oficial de Brasília.
- Anexa o arquivo JSON resultante da coleta para ingestão downstream.
"""

from datetime import datetime
import os
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
    """Envia sumário da execução regional e anexa arquivos JSON para os chats configurados."""
    raw_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip().strip('"').strip("'")
    chat_ids_raw = os.getenv("TELEGRAM_CHAT_ID", "").strip().strip('"').strip("'")

    if not raw_token:
        raise ValueError("A variável de ambiente TELEGRAM_BOT_TOKEN não foi definida.")
    if not chat_ids_raw:
        raise ValueError("A variável de ambiente TELEGRAM_CHAT_ID não foi definida.")

    # Sanitiza o token caso o usuário tenha colado com o prefixo 'bot'
    if raw_token.lower().startswith("bot"):
        bot_token = raw_token[3:]
    else:
        bot_token = raw_token

    chat_ids = [c.strip() for c in chat_ids_raw.split(",") if c.strip()]
    url_msg = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    url_doc = f"https://api.telegram.org/bot{bot_token}/sendDocument"

    taxa_sucesso = (sucessos / total_processadas * 100) if total_processadas > 0 else 0.0
    agora_bsb = datetime.now(FUSO_BRASILIA).strftime("%d/%m/%Y %H:%M:%S")

    resumo_msg = (
        f"<b>MONITORAMENTO DIÁRIO: {regiao_nome.upper()}</b>\n\n"
        f"<b>Data/Hora (BSB):</b> {agora_bsb}\n"
        f"<b>Janela:</b> Últimas 24 horas\n"
        f"<b>Matérias Brutas:</b> {total_brutas}\n"
        f"<b>Clusters Únicos:</b> {total_clusters}\n"
        f"<b>Textos Extraídos:</b> {sucessos} ({taxa_sucesso:.1f}%)\n"
        f"<b>Bloqueadas / Falhas:</b> {bloqueados}\n\n"
        f"<i>Arquivo JSON consolidado em anexo.</i>"
    )

    for chat_id in chat_ids:
        # Envio do texto resumido
        resp_msg = requests.post(
            url_msg,
            json={"chat_id": chat_id, "text": resumo_msg, "parse_mode": "HTML"},
            timeout=30,
        )
        if resp_msg.status_code != 200:
            raise RuntimeError(f"Falha ao enviar mensagem Telegram para {chat_id} (HTTP {resp_msg.status_code}): {resp_msg.text}")

        # Envio dos anexos JSON
        for caminho in arquivos:
            if not os.path.exists(caminho):
                continue
            with open(caminho, "rb") as doc:
                resp_doc = requests.post(
                    url_doc,
                    data={"chat_id": chat_id},
                    files={"document": doc},
                    timeout=60,
                )
                if resp_doc.status_code != 200:
                    raise RuntimeError(f"Falha ao enviar documento {caminho} para {chat_id} (HTTP {resp_doc.status_code}): {resp_doc.text}")
