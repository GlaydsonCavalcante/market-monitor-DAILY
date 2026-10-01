"""
test_telegram.py

Script de diagnóstico aprofundado para homologação da API do Telegram.
Inspeciona anatomia de credenciais, valida rotas e emite telemetria detalhada.
"""

from datetime import datetime
import json
import os
import re
import tempfile
from zoneinfo import ZoneInfo
import requests

FUSO_BRASILIA = ZoneInfo("America/Sao_Paulo")


def analisar_anatomia_token(token: str) -> None:
    """Imprime relatório detalhado da estrutura do token sem vazar dados sensíveis."""
    print("------------------------------------------------------------")
    print("ANÁLISE ESTRUTURAL DO TOKEN")
    print(f"• Tamanho total: {len(token)} caracteres (esperado: 45-46)")
    
    if ":" in token:
        partes = token.split(":", 1)
        prefixo_id = partes[0]
        sufixo_hash = partes[1]
        print(f"• Separador ':' detectado: SIM")
        print(f"• ID numérico do Bot: '{prefixo_id}' ({len(prefixo_id)} dígitos)")
        print(f"• Comprimento do Hash: {len(sufixo_hash)} caracteres (esperado: 35)")
        print(f"• Início do Hash: '{sufixo_hash[:4]}...' | Fim do Hash: '...{sufixo_hash[-3:]}'")
    else:
        print(f"• Separador ':' detectado: NÃO")
        print(f"• ⚠️ ALERTA: O token não possui o formato obrigatório '<ID>:<HASH>'.")
        print(f"• Amostra segura: '{token[:4]}...{token[-4:]}'")

    if len(token) == 32 or len(token) == 31:
        print("• ⚠️ SUSPEITA: Tamanho compatível com 'api_hash' do my.telegram.org.")
        print("  O Telegram Bot exige o token gerado exclusivamente pelo @BotFather.")
    print("------------------------------------------------------------")


def testar_conexao_telegram() -> None:
    raw_token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip().strip('"').strip("'")
    raw_chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip().strip('"').strip("'")

    if not raw_token:
        raise ValueError("A variável de ambiente TELEGRAM_BOT_TOKEN não foi encontrada.")
    if not raw_chat_id:
        raise ValueError("A variável de ambiente TELEGRAM_CHAT_ID não foi encontrada.")

    bot_token = raw_token[3:] if raw_token.lower().startswith("bot") else raw_token

    print("=" * 60)
    print("INICIANDO DIAGNÓSTICO AVANÇADO DO TELEGRAM")
    print("=" * 60)

    analisar_anatomia_token(bot_token)

    chat_ids = [c.strip() for c in raw_chat_id.split(",") if c.strip()]
    print(f"\nChats configurados para recebimento: {len(chat_ids)}")
    for i, cid in enumerate(chat_ids, 1):
        tipo_chat = "Canal/Supergrupo" if cid.startswith("-100") else ("Grupo Comum" if cid.startswith("-") else "Usuário Individual")
        print(f"  [{i}] ID: {cid} | Tipo detectado: {tipo_chat}")

    # 1. Consulta ao endpoint getMe
    url_get_me = f"https://api.telegram.org/bot{bot_token}/getMe"
    print(f"\n>> [Etapa 1/3] Testando rota getMe: https://api.telegram.org/bot<TOKEN>/getMe ...")
    
    resp_me = requests.get(url_get_me, timeout=15)
    print(f"• Código HTTP retornado: {resp_me.status_code}")
    print(f"• Resposta bruta da API: {resp_me.text}")

    if resp_me.status_code == 404:
        raise RuntimeError(
            f"Erro 404 (Not Found). O Telegram não reconheceu o token informado.\n"
            f"Verifique se o valor em TELEGRAM_BOT_TOKEN foi obtido diretamente do @BotFather."
        )
    if resp_me.status_code != 200:
        raise RuntimeError(f"Falha de autenticação (HTTP {resp_me.status_code}): {resp_me.text}")

    dados_bot = resp_me.json().get("result", {})
    print(f"✓ Identidade validada com sucesso:")
    print(f"  - Username: @{dados_bot.get('username')}")
    print(f"  - Nome: {dados_bot.get('first_name')}")
    print(f"  - ID do Bot: {dados_bot.get('id')}")

    agora_bsb = datetime.now(FUSO_BRASILIA).strftime("%d/%m/%Y %H:%M:%S")

    # 2. Teste de Mensagem (sendMessage)
    url_msg = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    texto_teste = (
        f"🧪 <b>TESTE DE INTEGRAÇÃO - MONITORAMENTO</b>\n\n"
        f"📅 <b>Data/Hora (BSB):</b> {agora_bsb}\n"
        f"🤖 <b>Bot Ativo:</b> @{dados_bot.get('username')}\n"
        f"✅ Autenticação e rotas HTTP homologadas com sucesso."
    )

    for cid in chat_ids:
        print(f"\n>> [Etapa 2/3] Enviando mensagem de teste para o chat: {cid}...")
        resp_msg = requests.post(
            url_msg,
            json={"chat_id": cid, "text": texto_teste, "parse_mode": "HTML"},
            timeout=15,
        )
        print(f"• Código HTTP: {resp_msg.status_code}")
        print(f"• Resposta da API: {resp_msg.text}")
        if resp_msg.status_code != 200:
            raise RuntimeError(f"Erro ao entregar mensagem para {cid}: {resp_msg.text}")
        print(f"✓ Mensagem de texto confirmada no destino.")

    # 3. Teste de Documento JSON (sendDocument)
    url_doc = f"https://api.telegram.org/bot{bot_token}/sendDocument"
    dados_mock = [
        {
            "teste_integracao": True,
            "servico": "market-monitor-daily",
            "timestamp": agora_bsb,
            "status": "HOMOLOGADO"
        }
    ]

    with tempfile.NamedTemporaryFile("w+", encoding="utf-8", suffix=".json", delete=False) as tmp:
        json.dump(dados_mock, tmp, indent=2, ensure_ascii=False)
        caminho_tmp = tmp.name

    try:
        for cid in chat_ids:
            print(f"\n>> [Etapa 3/3] Enviando documento de teste para o chat: {cid}...")
            with open(caminho_tmp, "rb") as arquivo_teste:
                resp_doc = requests.post(
                    url_doc,
                    data={"chat_id": cid, "caption": "📎 Homologação de anexo JSON."},
                    files={"document": ("homologacao_teste.json", arquivo_teste, "application/json")},
                    timeout=30,
                )
            print(f"• Código HTTP: {resp_doc.status_code}")
            print(f"• Resposta da API: {resp_doc.text}")
            if resp_doc.status_code != 200:
                raise RuntimeError(f"Erro ao enviar arquivo para {cid}: {resp_doc.text}")
            print(f"✓ Anexo entregue com sucesso.")
    finally:
        if os.path.exists(caminho_tmp):
            os.remove(caminho_tmp)

    print("\n" + "=" * 60)
    print("TODAS AS ETAPAS DE COMUNICAÇÃO COM O TELEGRAM FORAM HOMOLOGADAS.")
    print("=" * 60)


if __name__ == "__main__":
    testar_conexao_telegram()
