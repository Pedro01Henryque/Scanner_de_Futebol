import html
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from typing import List, Optional

import gspread
import requests
from google import genai
from google.genai import types
from google.oauth2.service_account import Credentials
from pydantic import BaseModel

# ==============================================================================
# CONFIGURAÇÕES DO RASTREADOR DE BANCA (COLUNAS A:F + STATUS "ABERTO" EM H)
# ==============================================================================

GEMINI_MODEL = "gemini-2.5-flash"
ABA_PLANILHA = "Página1"


class SelecaoBilhete(BaseModel):
    evento: str
    mercado: str
    odd: float


class ApostaExtraida(BaseModel):
    casa_aposta: str
    esporte: str
    selecoes: List[SelecaoBilhete]
    odd_total_explicitamente_visivel: Optional[float] = None
    valor_apostado: float
    resultado: str  # "Aberto", "Green", "Red" ou "Void"


# ==============================================================================
# CONEXÃO E UTILITÁRIOS DE PLANILHA
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


def primeira_linha_vazia_col_a(ws) -> int:
    """Retorna o número da primeira linha onde a Coluna A está vazia."""
    valores_col_a = ws.col_values(1)
    for idx, val in enumerate(valores_col_a, start=1):
        if not str(val).strip():
            return idx
    return len(valores_col_a) + 1


def formatar_brl_mensagem(valor: float) -> str:
    return f"R$ {valor:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def formatar_valor_planilha(valor: float) -> str:
    """Formata com 2 casas decimais e vírgula, SEM 'R$' (ex: 5,99 ou 5,00)."""
    return f"{valor:.2f}".replace(".", ",")


def formatar_odd_planilha(valor: float) -> str:
    """Formata odd com vírgula decimal (ex: 3,36 ou 2,5)."""
    return f"{valor:.2f}".rstrip("0").rstrip(".").replace(".", ",")


def parse_moeda_br(texto: str) -> float:
    if not texto:
        return 0.0
    limpo = re.sub(r"[R$\s.]", "", str(texto)).replace(",", ".")
    try:
        return float(limpo)
    except ValueError:
        return 0.0


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


def baixar_foto_telegram(token: str, file_id: str) -> tuple[Optional[bytes], str]:
    try:
        resp_info = requests.get(
            f"https://api.telegram.org/bot{token}/getFile",
            params={"file_id": file_id},
            timeout=15,
        )
        resp_info.raise_for_status()
        file_path = resp_info.json()["result"]["file_path"]
        mime = "image/png" if file_path.lower().endswith(".png") else "image/jpeg"

        resp_img = requests.get(
            f"https://api.telegram.org/file/bot{token}/{file_path}",
            timeout=20,
        )
        resp_img.raise_for_status()
        return resp_img.content, mime
    except Exception as e:
        print(f"[!] Erro ao baixar foto do Telegram: {e}")
        return None, "image/jpeg"


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
        mime_type = "image/jpeg"

        if "photo" in msg and msg["photo"]:
            maior_foto = msg["photo"][-1]
            foto_bytes, mime_type = baixar_foto_telegram(token, maior_foto["file_id"])

        if texto or foto_bytes:
            mensagens_validas.append(
                {
                    "texto": texto,
                    "foto_bytes": foto_bytes,
                    "mime_type": mime_type,
                    "timestamp": msg.get("date"),
                }
            )

    if ultimo_update_id is not None:
        requests.get(url, params={"offset": ultimo_update_id + 1}, timeout=15)

    return mensagens_validas


# ==============================================================================
# PARSING MULTIMODAL COM GEMINI (TODAS AS SELEÇÕES + CÁLCULO DE ODD EM PYTHON)
# ==============================================================================

def interpretar_aposta_gemini(
    client: genai.Client,
    texto_usuario: str,
    foto_bytes: Optional[bytes] = None,
    mime_type: str = "image/jpeg",
) -> Optional[ApostaExtraida]:
    prompt = f"""
    Você é um auditor meticuloso de bilhetes de apostas esportivas.
    Analise a imagem inteira (do topo até a borda inferior antes da barra de navegação) e a legenda do usuário.

    Legenda/Texto do usuário: "{texto_usuario}"

    REGRAS OBRIGATÓRIAS DE EXTRAÇÃO:
    1. VARREDURA COMPLETA DE SELEÇÕES (lista `selecoes`):
       - Olhe cada bloco/linha do bilhete de cima para baixo, inclusive a última linha próxima ao rodapé da imagem!
       - Para CADA seleção encontrada no bilhete (1, 2, 3 ou mais seleções), adicione um item separado em `selecoes`:
         * `evento`: Nome completo dos dois times/jogadores separados por " x " (ex: "Shelton, Ben x Rafael Jodar", "Harri Heliovaara / Patten H x Arribage T / Olivetti A"). Se na parte inferior da imagem aparecer apenas a escolha e o mercado (ex: "Zverev, Alexander/Zverev, Alexander - Hipótese dupla (1° set/partida)") porque o confronto ficou cortado abaixo, coloque a escolha no campo `evento`!
         * `mercado`: Escolha + mercado exato (ex: "Shelton, Ben - Vencedor", "Menos de 2.5", "Hipótese dupla (1° set/partida)").
         * `odd`: Odd decimal individual daquela seleção (ex: 1.33, 1.58, 1.60).
       - NUNCA pare na primeira seleção se houver outras abaixo dela!
    2. `casa_aposta`: Nome da casa (ex: Stake, Betano, Bet365, Betfair, Superbet). Identifique pela legenda ou logotipo/URL na imagem.
    3. `esporte`: Esporte principal (ex: Tênis, Futebol, NBA, NFL).
    4. `odd_total_explicitamente_visivel`: Preencha APENAS se houver um campo escrito "Odd total" ou "Cotações totais" no bilhete; caso contrário deixe null (o Python multiplicará `selecoes` automaticamente).
    5. `valor_apostado`: Valor apostado informado na legenda ou no campo de aposta do bilhete (ex: 5.0 ou 5.99). NUNCA pegue o saldo da carteira no topo da tela (ex: R$ 55,00)!
    6. `resultado`: Retorne EXATAMENTE "Aberto", "Green", "Red" ou "Void". Se estiver em "Apostas ativas" ou sem resultado final explícito na legenda, retorne "Aberto".
    """

    conteudos = [prompt]
    if foto_bytes:
        conteudos.append(types.Part.from_bytes(data=foto_bytes, mime_type=mime_type))

    try:
        resp = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=conteudos,
            config={
                "response_mime_type": "application/json",
                "response_schema": ApostaExtraida,
                "temperature": 0.05,
            },
        )
        return ApostaExtraida.model_validate_json(resp.text)
    except Exception as e:
        print(f"[!] Erro ao interpretar aposta com Gemini: {e}")
        return None


def montar_descricao_e_odd_total(aposta: ApostaExtraida) -> tuple[str, float]:
    selecoes = aposta.selecoes or []
    if len(selecoes) == 1:
        s = selecoes[0]
        descricao = f"{s.evento} - {s.mercado}"
        odd_calculada = s.odd
    else:
        partes = []
        produto_odds = 1.0
        for s in selecoes:
            partes.append(f"{s.evento} ({s.mercado} @{s.odd:.2f})")
            if s.odd > 0:
                produto_odds *= s.odd
        descricao = f"Múltipla ({len(selecoes)} seleções): " + " | ".join(partes)
        odd_calculada = round(produto_odds, 2)

    if aposta.odd_total_explicitamente_visivel and aposta.odd_total_explicitamente_visivel > 1.0:
        odd_final = round(aposta.odd_total_explicitamente_visivel, 2)
    else:
        odd_final = round(odd_calculada, 2)

    return descricao, odd_final


def atualizar_ultima_aberta(ws, novo_resultado: str) -> str:
    """Atualiza APENAS a coluna H (Resultado) da última aposta com status 'Aberto'."""
    todas_linhas = ws.get_all_values()
    for idx in range(len(todas_linhas) - 1, 0, -1):
        linha = todas_linhas[idx]
        if len(linha) >= 8 and linha[0].strip() and linha[7].strip().lower() in ("aberto", "pendente"):
            numero_linha = idx + 1
            res_norm = novo_resultado.strip().capitalize()

            ws.update(
                f"H{numero_linha}",
                [[res_norm]],
                value_input_option="USER_ENTERED",
            )
            emoji = "✅" if res_norm == "Green" else ("🔄" if res_norm == "Void" else "❌")
            return (
                f"{emoji} <b>Resultado Atualizado na Coluna H (Linha {numero_linha}):</b>\n"
                f"📌 {html.escape(linha[3])}\n"
                f"🏷️ Status: <b>{res_norm}</b> | Odd: <code>{linha[4]}</code> | Stake: <b>{linha[5]}</b>"
            )

    return "⚠️ Nenhuma aposta com status <b>Aberto</b> encontrada na planilha para atualizar."


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
    abertas = 0
    entradas_validas = 0

    total_apostado_mes = 0.0
    lucro_liquido_mes = 0.0
    greens_mes = 0
    reds_mes = 0

    for linha in linhas[1:]:
        if len(linha) < 8 or not linha[0].strip():
            continue
        entradas_validas += 1
        data_str = linha[0].strip()
        stake = parse_moeda_br(linha[5])
        resultado = linha[7].strip().capitalize()
        lucro_col = parse_moeda_br(linha[8]) if len(linha) >= 9 else 0.0

        if resultado in ("Aberto", "Pendente"):
            abertas += 1
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
        f"• Volume Apostado: <b>{formatar_brl_mensagem(total_apostado_mes)}</b>\n"
        f"• Lucro Líquido: <b>{formatar_brl_mensagem(lucro_liquido_mes)}</b> (ROI: <b>{roi_mes:+.1f}%</b>)\n"
        f"• Placar: <b>{greens_mes}G</b> / <b>{reds_mes}R</b>\n\n"
        f"🏦 <b>Acumulado Histórico ({entradas_validas} entradas):</b>\n"
        f"• Volume Total: <b>{formatar_brl_mensagem(total_apostado_geral)}</b>\n"
        f"• Lucro Líquido: <b>{formatar_brl_mensagem(lucro_liquido_geral)}</b> (ROI: <b>{roi_geral:+.2f}%</b>)\n"
        f"• Taxa de Acerto: <b>{winrate_geral:.1f}%</b> ({greens_geral}G / {reds_geral}R)\n"
        f"• Apostas em Aberto: <b>{abertas}</b>"
    )


def processar_mensagem(
    ws,
    client: genai.Client,
    texto: str,
    foto_bytes: Optional[bytes] = None,
    mime_type: str = "image/jpeg",
) -> str:
    txt_lower = texto.strip().lower()

    if not foto_bytes:
        if txt_lower in ("/start", "/ajuda", "ajuda"):
            return (
                "🤖 <b>Rastreador de Banca Ativo!</b>\n\n"
                "<b>1. Por foto:</b> Mande o print do bilhete com a legenda:\n"
                "<code>casa: Stake | valor: 5,99</code>\n\n"
                "<b>2. Por texto:</b>\n"
                "<code>Stake | Tênis | Shelton Vencedor | @1.33 | 5,99</code>\n\n"
                "<b>Outros comandos:</b>\n"
                "• <code>Green</code> / <code>Red</code> / <code>Void</code> — fecha a última aposta em Aberto\n"
                "• <code>/resumo</code> — exibe ROI e lucro do mês e histórico"
            )

        if txt_lower in ("/resumo", "/banca", "resumo", "banca", "roi"):
            return gerar_relatorio_banca(ws)

        if txt_lower in ("green", "red", "void", "reembolso"):
            status = "Void" if txt_lower == "reembolso" else txt_lower.capitalize()
            return atualizar_ultima_aberta(ws, status)

    aposta = interpretar_aposta_gemini(client, texto, foto_bytes, mime_type)
    if not aposta or not aposta.selecoes or aposta.valor_apostado <= 0:
        return (
            "⚠️ Não consegui identificar as seleções ou o <b>Valor Apostado</b>.\n"
            "Ao mandar print, escreva na legenda: <code>casa: Stake | valor: 5,99</code>"
        )

    descricao_final, odd_final = montar_descricao_e_odd_total(aposta)
    if odd_final <= 1.0:
        return "⚠️ Odd total inválida (menor ou igual a 1.00). Verifique o print ou informe na legenda."

    agora_brt = datetime.now(timezone.utc) - timedelta(hours=3)
    data_hoje = agora_brt.strftime("%d/%m/%Y")
    linha_destino = primeira_linha_vazia_col_a(ws)

    # Colunas A até F (Coluna F recebe valor numérico com vírgula SEM "R$", ex: 5,99)
    valores_a_f = [[
        data_hoje,
        aposta.casa_aposta,
        aposta.esporte,
        descricao_final,
        formatar_odd_planilha(odd_final),
        formatar_valor_planilha(aposta.valor_apostado),
    ]]
    ws.update(
        f"A{linha_destino}:F{linha_destino}",
        valores_a_f,
        value_input_option="USER_ENTERED",
    )

    # Coluna H recebe "Aberto", "Green", "Red" ou "Void"
    res_norm = aposta.resultado.strip().capitalize()
    if res_norm == "Pendente":
        res_norm = "Aberto"
    if res_norm not in ("Aberto", "Green", "Red", "Void"):
        res_norm = "Aberto"

    ws.update(
        f"H{linha_destino}",
        [[res_norm]],
        value_input_option="USER_ENTERED",
    )

    retorno_previsto = round(aposta.valor_apostado * odd_final, 2)

    return (
        f"📝 <b>Entrada Gravada na Linha {linha_destino}:</b>\n"
        f"🏛️ {html.escape(aposta.casa_aposta)} • 🏆 {html.escape(aposta.esporte)}\n"
        f"📌 <b>{html.escape(descricao_final)}</b>\n"
        f"🎯 Odd Total: <b>@{odd_final:.2f}</b> | Stake: <b>{formatar_valor_planilha(aposta.valor_apostado)}</b>\n"
        f"💵 Retorno Previsto: <b>{formatar_brl_mensagem(retorno_previsto)}</b> | Status (Col H): <b>{res_norm}</b>"
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
        resposta = processar_mensagem(ws, client, m["texto"], m["foto_bytes"], m["mime_type"])
        enviar_telegram(tg_token, tg_chat, resposta)


if __name__ == "__main__":
    main()
