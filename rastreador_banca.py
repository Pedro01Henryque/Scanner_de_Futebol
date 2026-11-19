import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

import gspread
import requests
from google import genai
from google.genai import types
from google.oauth2.service_account import Credentials
from pydantic import BaseModel

# ==============================================================================
# CONFIGURAÇÕES DO RASTREADOR DE BANCA (TEXTO + PRINT DE BILHETE)
# ==============================================================================

GEMINI_MODEL = "gemini-2.5-flash"
ABA_PLANILHA = "Página1"


class ApostaExtraida(BaseModel):
    casa_aposta: str
    esporte: str
    descricao_aposta: str
    odd: float
    valor_apostado: float
    resultado: str  # "Pendente", "Green", "Red" ou "Void"


# ==============================================================================
# CONEXÃO COM GOOGLE SHEETS
# ==============================================================================

def conectar_planilha(sheet_id: str, creds_json_str: str):
    creds_dict = json.loads(creds_json_str)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    planilha = gc.open_by_key(sheet_id)
    return planilha.worksheet(ABA_PLANILHA)


def formatar_brl(valor: float) -> str:
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def formatar_decimal_br(valor: float) -> str:
    return f"{valor:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def parse_moeda_br(texto: str) -> float:
    if not texto:
        return 0.0
    limpo = re.sub(r"[R$\s.]", "", str(texto)).replace(",", ".")
    try:
        return float(limpo)
    except ValueError:
        return 0.0


def calcular_valores_aposta(odd: float, stake: float, resultado: str):
    res_norm = resultado.strip().capitalize()
    retorno_esperado = round(stake * odd, 2)

    if res_norm == "Green":
        lucro = round(retorno_esperado - stake, 2)
        calculo = retorno_esperado
    elif res_norm == "Void":
        lucro = 0.0
        calculo = stake
    else:  # "Red" ou "Pendente"
        lucro = 0.0
        calculo = 0.0

    return res_norm, retorno_esperado, lucro, calculo


# ==============================================================================
# TELEGRAM API (TEXTO + DOWNLOAD DE FOTO DE BILHETE)
# ==============================================================================

def enviar_telegram(token: str, chat_id: str, texto_html: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    requests.post(
        url,
        json={
            "chat_id": chat_id,
            "text": texto_html,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        },
        timeout=20,
    )


def baixar_foto_telegram(token: str, file_id: str) -> Optional[bytes]:
    try:
        resp_info = requests.get(
            f"https://api.telegram.org/bot{token}/getFile",
            params={"file_id": file_id},
            timeout=15,
        )
        resp_info.raise_for_status()
        file_path = resp_info.json()["result"]["file_path"]

        resp_img = requests.get(
            f"https://api.telegram.org/file/bot{token}/{file_path}",
            timeout=20,
        )
        resp_img.raise_for_status()
        return resp_img.content
    except Exception as e:
        print(f"[!] Erro ao baixar foto do Telegram: {e}")
        return None


def buscar_mensagens_novas(token: str, chat_id_autorizado: str) -> list:
    url = f"https://api.telegram.org/bot{token}/getUpdates"
    resp = requests.get(url, params={"timeout": 5}, timeout=15)
    resp.raise_for_status()
    dados = resp.json()

    if not dados.get("ok"):
        return []

    updates = dados.get("result", [])
    if not updates:
        return []

    mensagens_validas = []
    ultimo_update_id = None

    for up in updates:
        ultimo_update_id = up["update_id"]
        msg = up.get("message") or up.get("edited_message")
        if not msg:
            continue

        remetente_id = str(msg["chat"]["id"])
        if remetente_id != str(chat_id_autorizado):
            continue

        texto = (msg.get("text") or msg.get("caption") or "").strip()
        foto_bytes = None

        if "photo" in msg and msg["photo"]:
            # Pega a versão de maior resolução (último item da lista)
            maior_foto = msg["photo"][-1]
            foto_bytes = baixar_foto_telegram(token, maior_foto["file_id"])

        if texto or foto_bytes:
            mensagens_validas.append(
                {
                    "texto": texto,
                    "foto_bytes": foto_bytes,
                    "timestamp": msg.get("date"),
                }
            )

    if ultimo_update_id is not None:
        requests.get(url, params={"offset": ultimo_update_id + 1}, timeout=15)

    return mensagens_validas


# ==============================================================================
# PARSING MULTIMODAL COM GEMINI (TEXTO E PRINT) E OPERAÇÕES NA PLANILHA
# ==============================================================================

def interpretar_aposta_gemini(
    client: genai.Client, texto_usuario: str, foto_bytes: Optional[bytes] = None
) -> Optional[ApostaExtraida]:
    prompt = f"""
    Extraia os dados desta aposta esportiva enviada pelo usuário no Telegram (via texto e/ou print do bilhete) para registro em planilha:
    Legenda/Texto do usuário: "{texto_usuario}"

    Regras obrigatórias:
    - casa_aposta: Nome da casa de apostas (ex: Stake, Betano, Bet365, Betfair, Superbet, Novibet). Identifique pela legenda ou pelo visual/URL da imagem.
    - esporte: Futebol, NBA, NFL, Tênis, F1, eSports, etc.
    - descricao_aposta:
      * Se for aposta simples: "Time A x Time B - Mercado" (ex: "Vasco x Internacional - Menos de 2.5").
      * Se for múltipla/combinada (2 ou mais seleções no print): liste todas as seleções separadas por " + " e indique Múltipla no início (ex: "Múltipla: Vasco x Internacional (Menos de 2.5) + Vitória x Bragantino (Menos de 2.5)").
    - odd: Odd total decimal do bilhete. Se houver odd total visível no print, use ela; se for múltipla e só aparecerem as odds individuais, multiplique-as (ex: 2.13 * 2.00 = 4.26).
    - valor_apostado: Valor numérico apostado em reais (ex: 5.00), extraído da legenda ou do print.
    - resultado: Exatamente "Green", "Red", "Void" ou "Pendente". Se o bilhete estiver ativo/aberto ou não houver resultado explícito, use "Pendente".
    """

    conteudos = [prompt]
    if foto_bytes:
        conteudos.append(types.Part.from_bytes(data=foto_bytes, mime_type="image/jpeg"))

    try:
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=conteudos,
            config={
                "response_mime_type": "application/json",
                "response_schema": ApostaExtraida,
            },
        )
        return ApostaExtraida.model_validate_json(resp.text)
    except Exception as e:
        print(f"[!] Erro ao interpretar aposta com Gemini: {e}")
        return None


def atualizar_ultima_pendente(ws, novo_resultado: str) -> str:
    todas_linhas = ws.get_all_values()
    for idx in range(len(todas_linhas) - 1, 0, -1):
        linha = todas_linhas[idx]
        if len(linha) >= 8 and linha[7].strip().lower() == "pendente":
            numero_linha = idx + 1
            odd = parse_moeda_br(linha[4])
            stake = parse_moeda_br(linha[5])
            res_norm, ret_esp, lucro, calculo = calcular_valores_aposta(odd, stake, novo_resultado)

            ws.update(
                f"H{numero_linha}:J{numero_linha}",
                [[res_norm, formatar_brl(lucro), formatar_decimal_br(calculo)]],
                value_input_option="USER_ENTERED",
            )
            emoji = "✅" if res_norm == "Green" else ("🔄" if res_norm == "Void" else "❌")
            return (
                f"{emoji} <b>Aposta Atualizada (Linha {numero_linha}):</b>\n"
                f"📌 {html.escape(linha[3])}\n"
                f"🏷️ Status: <b>{res_norm}</b> | Odd: <code>{linha[4]}</code> | Stake: <b>{linha[5]}</b>\n"
                f"💰 Lucro Líquido: <b>{formatar_brl(lucro)}</b>"
            )

    return "⚠️ Nenhuma aposta com status <b>Pendente</b> encontrada na planilha para atualizar."


def gerar_relatorio_banca(ws, titulo: str = "📊 RESUMO DE BANCA & ROI") -> str:
    linhas = ws.get_all_values()
    if len(linhas) <= 1:
        return "📭 Planilha ainda sem registros."

    agora_brt = datetime.now(timezone.utc) - timedelta(hours=3)
    mes_atual = agora_brt.strftime("/%m/%Y")

    total_apostado_geral = 0.0
    lucro_liquido_geral = 0.0
    greens_geral = 0
    reds_geral = 0
    pendentes = 0

    total_apostado_mes = 0.0
    lucro_liquido_mes = 0.0
    greens_mes = 0
    reds_mes = 0

    for linha in linhas[1:]:
        if len(linha) < 9:
            continue
        data_str = linha[0].strip()
        stake = parse_moeda_br(linha[5])
        resultado = linha[7].strip().capitalize()
        lucro_col = parse_moeda_br(linha[8])

        if resultado == "Pendente":
            pendentes += 1
            continue
        if resultado not in ("Green", "Red", "Void"):
            continue

        resultado_financeiro = lucro_col if resultado == "Green" else (-stake if resultado == "Red" else 0.0)

        total_apostado_geral += stake
        lucro_liquido_geral += resultado_financeiro
        if resultado == "Green":
            greens_geral += 1
        elif resultado == "Red":
            reds_geral += 1

        if data_str.endswith(mes_atual):
            total_apostado_mes += stake
            lucro_liquido_mes += resultado_financeiro
            if resultado == "Green":
                greens_mes += 1
            elif resultado == "Red":
                reds_mes += 1

    roi_geral = (lucro_liquido_geral / total_apostado_geral * 100) if total_apostado_geral > 0 else 0.0
    roi_mes = (lucro_liquido_mes / total_apostado_mes * 100) if total_apostado_mes > 0 else 0.0
    winrate_geral = (greens_geral / (greens_geral + reds_geral) * 100) if (greens_geral + reds_geral) > 0 else 0.0

    return (
        f"<b>{titulo}</b>\n\n"
        f"📅 <b>Mês Atual ({agora_brt.strftime('%m/%Y')}):</b>\n"
        f"• Volume Apostado: <b>{formatar_brl(total_apostado_mes)}</b>\n"
        f"• Lucro Líquido: <b>{formatar_brl(lucro_liquido_mes)}</b> (ROI: <b>{roi_mes:+.1f}%</b>)\n"
        f"• Placar: <b>{greens_mes}G</b> / <b>{reds_mes}R</b>\n\n"
        f"🏦 <b>Acumulado Histórico ({len(linhas) - 1} entradas):</b>\n"
        f"• Volume Total: <b>{formatar_brl(total_apostado_geral)}</b>\n"
        f"• Lucro Líquido: <b>{formatar_brl(lucro_liquido_geral)}</b> (ROI: <b>{roi_geral:+.2f}%</b>)\n"
        f"• Taxa de Acerto: <b>{winrate_geral:.1f}%</b> ({greens_geral}G / {reds_geral}R)\n"
        f"• Apostas Pendentes: <b>{pendentes}</b>"
    )


def processar_mensagem(ws, client: genai.Client, texto: str, foto_bytes: Optional[bytes] = None) -> str:
    txt_lower = texto.strip().lower()

    if not foto_bytes:
        if txt_lower in ("/start", "/ajuda", "ajuda"):
            return (
                "🤖 <b>Rastreador de Banca Ativo (Texto + Print)!</b>\n\n"
                "<b>1. Por foto:</b> Mande o print do bilhete com a legenda:\n"
                "<code>casa: Stake | valor: 5,00</code>\n\n"
                "<b>2. Por texto:</b>\n"
                "<code>Stake | Futebol | Vasco x Inter Under 2.5 | @2.13 | R$ 5</code>\n\n"
                "<b>Outros comandos:</b>\n"
                "• <code>Green</code> / <code>Red</code> / <code>Void</code> — fecha a última aposta pendente\n"
                "• <code>/resumo</code> — exibe ROI e lucro do mês e histórico"
            )

        if txt_lower in ("/resumo", "/banca", "resumo", "banca", "roi"):
            return gerar_relatorio_banca(ws)

        if txt_lower in ("green", "red", "void", "reembolso"):
            status = "Void" if txt_lower == "reembolso" else txt_lower.capitalize()
            return atualizar_ultima_pendente(ws, status)

    aposta = interpretar_aposta_gemini(client, texto, foto_bytes)
    if not aposta or aposta.odd <= 1.0 or aposta.valor_apostado <= 0:
        return (
            "⚠️ Não consegui identificar a <b>Odd</b> ou o <b>Valor Apostado</b>.\n"
            "Ao mandar print, escreva na legenda: <code>casa: Stake | valor: 5,00</code>"
        )

    agora_brt = datetime.now(timezone.utc) - timedelta(hours=3)
    data_hoje = agora_brt.strftime("%d/%m/%Y")
    res_norm, ret_esp, lucro, calculo = calcular_valores_aposta(aposta.odd, aposta.valor_apostado, aposta.resultado)

    nova_linha = [
        data_hoje,
        aposta.casa_aposta,
        aposta.esporte,
        aposta.descricao_aposta,
        formatar_decimal_br(aposta.odd),
        formatar_brl(aposta.valor_apostado),
        formatar_brl(ret_esp),
        res_norm,
        formatar_brl(lucro),
        formatar_decimal_br(calculo),
    ]

    ws.append_row(nova_linha, value_input_option="USER_ENTERED")
    total_linhas = len(ws.get_all_values())

    return (
        f"📝 <b>Entrada Registrada (Linha {total_linhas}):</b>\n"
        f"🏛️ {html.escape(aposta.casa_aposta)} • 🏆 {html.escape(aposta.esporte)}\n"
        f"📌 <b>{html.escape(aposta.descricao_aposta)}</b>\n"
        f"🎯 Odd Total: <b>@{aposta.odd:.2f}</b> | Stake: <b>{formatar_brl(aposta.valor_apostado)}</b>\n"
        f"💵 Retorno Esperado: <b>{formatar_brl(ret_esp)}</b> | Status: <b>{res_norm}</b>"
    )


def main():
    tg_token = os.environ.get("TELEGRAM_BANCA_TOKEN")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID")
    gsheets_id = os.environ.get("GSHEETS_ID")
    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    gemini_key = os.environ.get("GEMINI_API_KEY")

    if not all([tg_token, tg_chat, gsheets_id, creds_json, gemini_key]):
        raise EnvironmentError("Faltam Secrets obrigatórios para o Rastreador de Banca.")

    ws = conectar_planilha(gsheets_id, creds_json)

    if len(sys.argv) > 1 and sys.argv[1] == "--relatorio":
        relatorio = gerar_relatorio_banca(ws, "📈 BOLETIM SEMANAL DE PERFORMANCE")
        enviar_telegram(tg_token, tg_chat, relatorio)
        print("[Concluído] Relatório semanal enviado.")
        return

    mensagens = buscar_mensagens_novas(tg_token, tg_chat)
    if not mensagens:
        print("Nenhuma mensagem nova no bot de banca.")
        return

    client = genai.Client(http_options={"api_version": "v1beta"})
    for m in mensagens:
        print(f"Processando entrada (foto={bool(m['foto_bytes'])}): {m['texto']}")
        resposta = processar_mensagem(ws, client, m["texto"], m["foto_bytes"])
        enviar_telegram(tg_token, tg_chat, resposta)


if __name__ == "__main__":
    main()
