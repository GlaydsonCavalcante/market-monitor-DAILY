"""
test_telegram.py

Script de diagnóstico e homologação da API do Telegram.
Executa três verificações sequenciais sem mascaramento de exceções:
1. Validação estrutural das credenciais de ambiente.
2. Handshake com o endpoint getMe para testar a existência do bot.
3. Envio de mensagem de texto e documento JSON de teste.
"""

from datetime import datetime
import json
import os
import re
import tempfile
from zoneinfo import ZoneInfo
import requests

FUSO_BRASILIA = ZoneInfo("America/Sao_Paulo")


def testar_conexao_telegram() -> None:
    raw_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip().strip('"').strip("'")
    raw_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip().strip('"').strip("'")

    if not raw_token:
        raise ValueError("A variável de ambiente TELEGRAM_BOT_TOKEN não foi encontrada.")
    if not raw_chat_id:
        raise ValueError("A variável de ambiente TELEGRAM_CHAT_ID não foi encontrada.")

    # Remove eventual prefixo 'bot' duplicado
    bot_token = raw_token[3:] if raw_token.lower().startswith("bot") else raw_token

    print("=" * 60)
    print("DIAGNÓSTICO DE CREDENCIAIS TELEGRAM")
    print(f"Token - Tamanho: {len(bot_token)} caracteres")
    print(f"Token - Início: {bot_token[:5]}... | Fim: ...{bot_token[-4:]}")
    print(f"Chat ID bruto: {raw_chat_id}")
    print("=" * 60)

    # 1. Teste de Identidade do Bot (getMe)
    url_get_me = f"https://api.telegram.org/bot{bot_token}/getMe"
    print(f"\n>> [1/3] Consultando identidade do bot em: {url_get_me[:35]}...")
    resp_me = requests.get(url_get_me, timeout=15)

    if resp_me.status_code == 404:
        raise RuntimeError(
            f"Erro 404 ao consultar getMe. O Telegram rejeitou o token.\n"
            f"Resposta da API: {resp_me.text}\n"
            f"Ação corretiva: Acesse o @BotFather, recupere o token exato e atualize o secret TELEGRAM_BOT_TOKEN."
        )
    if resp_me.status_code != 200:
        raise RuntimeError(f"Falha na validação do bot (HTTP {resp_me.status_code}): {resp_me.text}")

    dados_bot = resp_me.json().get("result", {})
    print(f"✓ Bot validado: @{dados_bot.get('username')} ({dados_bot.get('first_name')}) [ID: {dados_bot.get('id')}]")

    chat_ids = [c.strip() for c in raw_chat_id.split(",") if c.strip()]
    agora_bsb = datetime.now(FUSO_BRASILIA).strftime("%d/%m/%Y %H:%M:%S")

    # 2. Teste de Envio de Mensagem de Texto (sendMessage)
    url_msg = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    texto_teste = (
        f"🧪 <b>TESTE DE INTEGRAÇÃO - MONITORAMENTO</b>\n\n"
        f"📅 <b>Data/Hora (BSB):</b> {agora_bsb}\n"
        f"🤖 <b>Bot:</b> @{dados_bot.get('username')}\n"
        f"✅ Handshake e autenticação estabelecidos com sucesso."
    )

    for cid in chat_ids:
        print(f"\n>> [2/3] Enviando mensagem de teste para o chat: {cid}...")
        resp_msg = requests.post(
            url_msg,
            json={"chat_id": cid, "text": texto_teste, "parse_mode": "HTML"},
            timeout=15,
        )
        if resp_msg.status_code != 200:
            raise RuntimeError(f"Falha no envio de texto para {cid} (HTTP {resp_msg.status_code}): {resp_msg.text}")
        print(f"✓ Mensagem de texto entregue com sucesso para {cid}.")

    # 3. Teste de Envio de Arquivo Anexo (sendDocument)
    url_doc = f"https://api.telegram.org/bot{bot_token}/sendDocument"
    dados_mock = [
        {
            "teste": True,
            "servico": "market-monitor-daily",
            "timestamp": agora_bsb,
            "status": "HOMOLOGADO",
        }
    ]

    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", suffix=".json", delete=False) as tmp:
        json.dump(dados_mock, tmp, indent=2, ensure_ascii=False)
        caminho_tmp = tmp.name

    try:
        for cid in chat_ids:
            print(f"\n>> [3/3] Enviando anexo JSON de teste para o chat: {cid}...")
            with open(caminho_tmp, "rb") as arquivo_teste:
                resp_doc = requests.post(
                    url_doc,
                    data={"chat_id": cid, "caption": "📎 Arquivo de homologação técnica."},
                    files={"document": ("teste_integracao.json", arquivo_teste, "application/json")},
                    timeout=30,
                )
            if resp_doc.status_code != 200:
                raise RuntimeError(f"Falha no envio de arquivo para {cid} (HTTP {resp_doc.status_code}): {resp_doc.text}")
            print(f"✓ Documento JSON entregue com sucesso para {cid}.")
    finally:
        if os.path.exists(caminho_tmp):
            os.remove(caminho_tmp)

    print("\n" + "=" * 60)
    print("TODOS OS TESTES DO TELEGRAM FORAM CONCLUÍDOS COM SUCESSO.")
    print("=" * 60)


if __name__ == "__main__":
    testar_conexao_telegram()
