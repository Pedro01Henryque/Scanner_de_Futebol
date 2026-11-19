import html
import os
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

import pandas as pd
import requests
from google import genai

# ==============================================================================
# CONFIGURAÇÕES DO SCANNER
# ==============================================================================

GEMINI_MODEL = "gemini-2.5-flash"
ARQUIVO_SAIDA = f"scanner_futebol_{datetime.now().strftime('%Y-%m-%d')}.xlsx"

# Ligas monitoradas na The Odds API + fontes abertas de resultados da temporada
LIGAS_ALVO = [
    {
        "nome": "Brasileirão Série A",
        "odds_key": "soccer_brazil_campeonato",
        "csv_historico": "https://www.football-data.co.uk/new/BRA.csv",
        "formato_novo": True,
    },
    {
        "nome": "Premier League",
        "odds_key": "soccer_epl",
        "csv_historico": "https://www.football-data.co.uk/mmz4281/2627/E0.csv",
        "formato_novo": False,
    },
    {
        "nome": "La Liga",
        "odds_key": "soccer_spain_la_liga",
        "csv_historico": "https://www.football-data.co.uk/mmz4281/2627/SP1.csv",
        "formato_novo": False,
    },
    {
        "nome": "Serie A (Itália)",
        "odds_key": "soccer_italy_serie_a",
        "csv_historico": "https://www.football-data.co.uk/mmz4281/2627/I1.csv",
        "formato_novo": False,
    },
    {
        "nome": "Bundesliga",
        "odds_key": "soccer_germany_bundesliga",
        "csv_historico": "https://www.football-data.co.uk/mmz4281/2627/D1.csv",
        "formato_novo": False,
    },
]

# Mapeamento de nomes de times (Odds API -> Football-Data CSV)
ALIASES_TIMES = {
    "atletico mineiro": "atletico-mg",
    "athletico paranaense": "athletico-pr",
    "red bull bragantino": "bragantino",
    "vasco da gama": "vasco",
    "manchester united": "man united",
    "manchester city": "man city",
    "nottingham forest": "nott'm forest",
    "newcastle united": "newcastle",
    "tottenham hotspur": "tottenham",
    "west ham united": "west ham",
    "wolverhampton wanderers": "wolves",
    "atletico madrid": "ath madrid",
    "athletic bilbao": "ath bilbao",
    "real betis": "betis",
    "real sociedad": "sociedad",
    "inter milan": "inter",
    "ac milan": "milan",
    "bayern munich": "bayern munich",
    "borussia dortmund": "dortmund",
    "bayer leverkusen": "leverkusen",
}


# ==============================================================================
# UTILITÁRIOS DE TEXTO E TELEGRAM
# ==============================================================================

def normalizar_time(nome: str) -> str:
    nfkd = unicodedata.normalize("NFKD", str(nome))
    sem_acento = "".join([c for c in nfkd if not unicodedata.combining(c)])
    limpo = re.sub(r"[^a-z0-9\s'-]", "", sem_acento.lower()).strip()
    return ALIASES_TIMES.get(limpo, limpo)


def enviar_telegram_texto(token: str, chat_id: str, mensagem_html: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    limite = 4000
    partes = [mensagem_html[i : i + limite] for i in range(0, len(mensagem_html), limite)]
    for parte in partes:
        resp = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": parte,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        if not resp.ok:
            print(f"[!] Erro Telegram texto: {resp.text}")


def enviar_telegram_arquivo(token: str, chat_id: str, caminho: str, legenda: str) -> None:
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    with open(caminho, "rb") as f:
        resp = requests.post(
            url,
            data={"chat_id": chat_id, "caption": legenda},
            files={"document": f},
            timeout=30,
        )
    if not resp.ok:
        print(f"[!] Erro Telegram arquivo: {resp.text}")


# ==============================================================================
# COLETA DE ESTATÍSTICAS HISTÓRICAS E ODDS DO DIA
# ==============================================================================

def carregar_estatisticas_liga(url_csv: str, formato_novo: bool) -> Dict[str, dict]:
    """Calcula médias casa/fora, frequência de Over 2.5 e Ambas Marcam (BTTS) dos últimos jogos."""
    try:
        df = pd.read_csv(url_csv)
    except Exception as e:
        print(f"   [!] Não foi possível ler CSV histórico ({url_csv}): {e}")
        return {}

    if formato_novo:
        # Formato BRA.csv (Home, Away, HG, AG)
        col_h, col_a, col_hg, col_ag = "Home", "Away", "HG", "AG"
        df = df.tail(380)  # Última temporada completa/atual
    else:
        # Formato europeu (HomeTeam, AwayTeam, FTHG, FTAG)
        col_h, col_a, col_hg, col_ag = "HomeTeam", "AwayTeam", "FTHG", "FTAG"

    df = df.dropna(subset=[col_h, col_a, col_hg, col_ag]).copy()
    df["total_gols"] = df[col_hg] + df[col_ag]
    df["over25"] = (df["total_gols"] > 2.5).astype(int)
    df["btts"] = ((df[col_hg] > 0) & (df[col_ag] > 0)).astype(int)

    stats_times = {}
    times_unicos = set(df[col_h]).union(set(df[col_a]))

    for time in times_unicos:
        jogos_casa = df[df[col_h] == time].tail(10)
        jogos_fora = df[df[col_a] == time].tail(10)

        if len(jogos_casa) < 3 or len(jogos_fora) < 3:
            continue

        chave = normalizar_time(time)
        stats_times[chave] = {
            "nome_csv": time,
            "casa_gols_pro": round(jogos_casa[col_hg].mean(), 2),
            "casa_gols_sofridos": round(jogos_casa[col_ag].mean(), 2),
            "casa_over25_%": round(jogos_casa["over25"].mean() * 100, 1),
            "casa_btts_%": round(jogos_casa["btts"].mean() * 100, 1),
            "casa_vitorias_%": round((jogos_casa[col_hg] > jogos_casa[col_ag]).mean() * 100, 1),
            "fora_gols_pro": round(jogos_fora[col_ag].mean(), 2),
            "fora_gols_sofridos": round(jogos_fora[col_hg].mean(), 2),
            "fora_over25_%": round(jogos_fora["over25"].mean() * 100, 1),
            "fora_btts_%": round(jogos_fora["btts"].mean() * 100, 1),
            "fora_vitorias_%": round((jogos_fora[col_ag] > jogos_fora[col_hg]).mean() * 100, 1),
        }

    return stats_times


def buscar_odds_liga(api_key: str, odds_key: str) -> List[dict]:
    """Busca jogos das próximas 30 horas com odds de Moneyline (1x2) e Over/Under 2.5."""
    url = f"https://api.the-odds-api.com/v4/sports/{odds_key}/odds"
    agora_utc = datetime.now(timezone.utc)
    limite_utc = agora_utc + timedelta(hours=30)

    params = {
        "apiKey": api_key,
        "regions": "eu,uk",
        "markets": "h2h,totals",
        "oddsFormat": "decimal",
        "commenceTimeFrom": agora_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "commenceTimeTo": limite_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
    }

    try:
        resp = requests.get(url, params=params, timeout=20)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        print(f"   [!] Erro ao buscar odds para {odds_key}: {e}")
        return []


def extrair_melhores_odds_evento(evento: dict) -> dict:
    """Pega a melhor odd disponível entre as casas para Casa, Empate, Fora, Over 2.5 e Under 2.5."""
    melhores = {
        "odd_casa": None,
        "odd_empate": None,
        "odd_fora": None,
        "odd_over25": None,
        "odd_under25": None,
    }
    home = evento.get("home_team")
    away = evento.get("away_team")

    for bookie in evento.get("bookmakers", []):
        for market in bookie.get("markets", []):
            key = market.get("key")
            outcomes = market.get("outcomes", [])
            if key == "h2h":
                for o in outcomes:
                    preco = float(o["price"])
                    if o["name"] == home and (not melhores["odd_casa"] or preco > melhores["odd_casa"]):
                        melhores["odd_casa"] = preco
                    elif o["name"] == away and (not melhores["odd_fora"] or preco > melhores["odd_fora"]):
                        melhores["odd_fora"] = preco
                    elif o["name"] == "Draw" and (not melhores["odd_empate"] or preco > melhores["odd_empate"]):
                        melhores["odd_empate"] = preco
            elif key == "totals":
                for o in outcomes:
                    if o.get("point") == 2.5:
                        preco = float(o["price"])
                        if o["name"] == "Over" and (not melhores["odd_over25"] or preco > melhores["odd_over25"]):
                            melhores["odd_over25"] = preco
                        elif o["name"] == "Under" and (not melhores["odd_under25"] or preco > melhores["odd_under25"]):
                            melhores["odd_under25"] = preco
    return melhores


# ==============================================================================
# MOTOR DE CRUZAMENTO E CÁLCULO DE +EV
# ==============================================================================

def encontrar_stats_time(nome_api: str, base_stats: Dict[str, dict]) -> Optional[dict]:
    norm = normalizar_time(nome_api)
    if norm in base_stats:
        return base_stats[norm]
    # Busca parcial caso haja prefixo/sufixo (ex: "FC Barcelona" -> "barcelona")
    for k, v in base_stats.items():
        if k in norm or norm in k:
            return v
    return None


def analisar_oportunidades_dia(odds_api_key: str) -> pd.DataFrame:
    registros = []

    for liga in LIGAS_ALVO:
        print(f"\n[Analisando] {liga['nome']}...")
        eventos = buscar_odds_liga(odds_api_key, liga["odds_key"])
        if not eventos:
            print("   -> Nenhum jogo nas próximas 30h.")
            continue

        stats_liga = carregar_estatisticas_liga(liga["csv_historico"], liga["formato_novo"])
        print(f"   -> {len(eventos)} jogo(s) encontrado(s) | {len(stats_liga)} times mapeados.")

        for ev in eventos:
            home = ev["home_team"]
            away = ev["away_team"]
            horario_utc = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
            horario_brt = (horario_utc - timedelta(hours=3)).strftime("%d/%m %H:%M")

            odds = extrair_melhores_odds_evento(ev)
            st_home = encontrar_stats_time(home, stats_liga)
            st_away = encontrar_stats_time(away, stats_liga)

            # Modelo simples de probabilidade combinada Casa + Fora
            if st_home and st_away:
                prob_over25_est = round((st_home["casa_over25_%"] + st_away["fora_over25_%"]) / 2, 1)
                prob_btts_est = round((st_home["casa_btts_%"] + st_away["fora_btts_%"]) / 2, 1)
                xg_projetado = round(
                    (
                        st_home["casa_gols_pro"]
                        + st_away["fora_gols_sofridos"]
                        + st_away["fora_gols_pro"]
                        + st_home["casa_gols_sofridos"]
                    )
                    / 2,
                    2,
                )
            else:
                prob_over25_est = None
                prob_btts_est = None
                xg_projetado = None

            # Calcula +EV no mercado de Over 2.5 Gols
            ev_over25_pct = None
            prob_implicita_over = None
            if prob_over25_est and odds["odd_over25"]:
                prob_implicita_over = round((1 / odds["odd_over25"]) * 100, 1)
                ev_over25_pct = round(prob_over25_est - prob_implicita_over, 1)

            registros.append(
                {
                    "liga": liga["nome"],
                    "horario_brt": horario_brt,
                    "jogo": f"{home} x {away}",
                    "odd_casa": odds["odd_casa"],
                    "odd_empate": odds["odd_empate"],
                    "odd_fora": odds["odd_fora"],
                    "odd_over25": odds["odd_over25"],
                    "prob_over25_est_%": prob_over25_est,
                    "prob_implicita_odd_%": prob_implicita_over,
                    "edge_over25_%": ev_over25_pct,
                    "prob_btts_est_%": prob_btts_est,
                    "media_gols_projetada": xg_projetado,
                }
            )

    return pd.DataFrame(registros)


# ==============================================================================
# SÍNTESE COM GEMINI E DISPARO NO TELEGRAM
# ==============================================================================

def gerar_resumo_gemini(client: genai.Client, df_destaques: pd.DataFrame) -> str:
    tabela_txt = df_destaques.to_string(index=False)
    prompt = f"""
    Você é um analista quantitativo de apostas esportivas.
    Analise esta tabela de jogos filtrados hoje com Edge (+EV) positivo no mercado de gols:
    
    {tabela_txt}
    
    Escreva um resumo objetivo em português (máximo 12 linhas, usando tags HTML simples <b> e <i> para Telegram)
    destacando os 3 melhores jogos do dia, explicando brevemente por que a média de gols projetada e a frequência histórica
    justificam valor frente à odd aberta. Sem floreios ou introduções genéricas.
    """
    try:
        resp = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        return resp.text.strip()
    except Exception as e:
        print(f"[!] Erro ao gerar comentário Gemini: {e}")
        return ""


def main():
    odds_key = os.environ.get("ODDS_API_KEY")
    gemini_key = os.environ.get("GEMINI_API_KEY")
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID")

    if not odds_key:
        raise EnvironmentError("Defina ODDS_API_KEY nos Secrets do GitHub.")

    df_jogos = analisar_oportunidades_dia(odds_key)
    data_hoje = datetime.now().strftime("%d/%m/%Y")

    if df_jogos.empty:
        print("\n[Info] Nenhum jogo encontrado nas ligas monitoradas hoje.")
        if tg_token and tg_chat:
            enviar_telegram_texto(
                tg_token,
                tg_chat,
                f"⚽ <b>Scanner Futebol ({data_hoje})</b>\n\nSem jogos das ligas monitoradas nas próximas 30 horas.",
            )
        return

    df_jogos = df_jogos.sort_values(by="edge_over25_%", ascending=False, na_position="last")
    df_destaques = df_jogos[df_jogos["edge_over25_%"].notna() & (df_jogos["edge_over25_%"] >= 5.0)]

    with pd.ExcelWriter(ARQUIVO_SAIDA, engine="openpyxl") as writer:
        df_destaques.to_excel(writer, sheet_name="Oportunidades +EV", index=False)
        df_jogos.to_excel(writer, sheet_name="Grade Completa", index=False)

    if not tg_token or not tg_chat:
        print("[Aviso] Telegram não configurado.")
        return

    linhas = [f"⚽ <b>SCANNER MATINAL DE VALOR (+EV) — {data_hoje}</b>\n"]

    if df_destaques.empty:
        linhas.append(
            f"Foram analisados <b>{len(df_jogos)} jogos</b> hoje, mas nenhum apresentou Edge ≥ +5% em Over 2.5 Gols."
        )
    else:
        linhas.append(f"🔥 <b>{len(df_destaques)} jogos com Edge positivo encontrados:</b>\n")
        for _, r in df_destaques.iterrows():
            jogo = html.escape(str(r["jogo"]))
            liga = html.escape(str(r["liga"]))
            linhas.append(
                f"• <b>{jogo}</b> ({liga} • {r['horario_brt']})\n"
                f"  Odd Over 2.5: <b>@{r['odd_over25']}</b> | Hist: <b>{r['prob_over25_est_%']}%</b> "
                f"(Edge: <b>+{r['edge_over25_%']}%</b> | BTTS: {r['prob_btts_est_%']}%)"
            )

        if gemini_key:
            client = genai.Client(http_options={"api_version": "v1beta"})
            analise_ia = gerar_resumo_gemini(client, df_destaques.head(5))
            if analise_ia:
                linhas.append(f"\n🧠 <b>Leitura Rápida IA:</b>\n{analise_ia}")

    enviar_telegram_texto(tg_token, tg_chat, "\n".join(linhas))
    enviar_telegram_arquivo(
        tg_token,
        tg_chat,
        ARQUIVO_SAIDA,
        f"📊 Grade completa ({len(df_jogos)} jogos) e métricas - {data_hoje}",
    )
    print("\n[Concluído] Boletim e planilha enviados para o Telegram!")


if __name__ == "__main__":
    main()
