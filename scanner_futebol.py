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
# CONFIGURAÇÕES DO SCANNER (BRASILEIRÃO + BIG 5 EUROPEIAS + CHAMPIONS + MLS)
# ==============================================================================

GEMINI_MODEL = "gemini-2.5-flash"

LIGAS_ALVO = [
    {
        "nome": "Brasileirão Série A",
        "odds_key": "soccer_brazil_campeonato",
        "csv_historico": "https://www.football-data.co.uk/new/BRA.csv",
        "formato_novo": True,
    },
    {
        "nome": "MLS (EUA)",
        "odds_key": "soccer_usa_mls",
        "csv_historico": "https://www.football-data.co.uk/new/USA.csv",
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
    {
        "nome": "Ligue 1 (França)",
        "odds_key": "soccer_france_ligue_one",
        "csv_historico": "https://www.football-data.co.uk/mmz4281/2627/F1.csv",
        "formato_novo": False,
    },
    {
        "nome": "UEFA Champions League",
        "odds_key": "soccer_uefa_champs_league",
        "csv_historico": None,
        "formato_novo": False,
    },
]

ALIASES_TIMES = {
    # Brasil
    "atletico mineiro": "atletico-mg",
    "athletico paranaense": "athletico-pr",
    "red bull bragantino": "bragantino",
    "vasco da gama": "vasco",
    # MLS (EUA)
    "inter miami cf": "inter miami",
    "los angeles fc": "los angeles fc",
    "la galaxy": "los angeles galaxy",
    "new york city fc": "new york city",
    "new york red bulls": "new york red bulls",
    "atlanta united fc": "atlanta utd",
    "orlando city sc": "orlando city",
    "columbus crew": "columbus crew",
    "fc cincinnati": "cincinnati",
    "seattle sounders fc": "seattle sounders",
    "minnesota united fc": "minnesota united",
    "real salt lake": "real salt lake",
    "houston dynamo fc": "houston dynamo",
    "vancouver whitecaps fc": "vancouver whitecaps",
    "charlotte fc": "charlotte",
    "cf montreal": "montreal",
    "toronto fc": "toronto fc",
    "philadelphia union": "philadelphia union",
    "dc united": "dc united",
    "chicago fire fc": "chicago fire",
    "new england revolution": "new england revolution",
    "nashville sc": "nashville sc",
    "sporting kansas city": "sporting kansas city",
    "st louis city sc": "st. louis city",
    "colorado rapids": "colorado rapids",
    "fc dallas": "fc dallas",
    "austin fc": "austin fc",
    "portland timbers": "portland timbers",
    "san jose earthquakes": "san jose earthquakes",
    # Inglaterra
    "manchester united": "man united",
    "manchester city": "man city",
    "nottingham forest": "nott'm forest",
    "newcastle united": "newcastle",
    "tottenham hotspur": "tottenham",
    "west ham united": "west ham",
    "wolverhampton wanderers": "wolves",
    "brighton and hove albion": "brighton",
    "leicester city": "leicester",
    "ipswich town": "ipswich",
    # Espanha
    "atletico madrid": "ath madrid",
    "athletic bilbao": "ath bilbao",
    "real betis": "betis",
    "real sociedad": "sociedad",
    "celta vigo": "celta",
    "rayo vallecano": "vallecano",
    "deportivo alaves": "alaves",
    # Itália
    "inter milan": "inter",
    "ac milan": "milan",
    "as roma": "roma",
    "ss lazio": "lazio",
    "hellas verona": "verona",
    # Alemanha
    "bayern munich": "bayern munich",
    "borussia dortmund": "dortmund",
    "bayer leverkusen": "leverkusen",
    "borussia monchengladbach": "m'gladbach",
    "eintracht frankfurt": "ein frankfurt",
    "rb leipzig": "rb leipzig",
    "vfb stuttgart": "stuttgart",
    "sc freiburg": "freiburg",
    "mainz 05": "mainz",
    # França
    "paris saint germain": "paris sg",
    "paris saint-germain": "paris sg",
    "olympique marseille": "marseille",
    "olympique lyonnais": "lyon",
    "as monaco": "monaco",
    "stade rennais": "rennes",
    "stade brestois": "brest",
    "rc lens": "lens",
    "losc lille": "lille",
    "ogc nice": "nice",
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
    """Envia mensagens formatadas em HTML para o Telegram, dividindo com segurança se passar de 3800 chars."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    limite = 3800

    blocos = mensagem_html.split("\n\n")
    parte_atual = ""

    for bloco in blocos:
        if len(parte_atual) + len(bloco) + 2 <= limite:
            parte_atual += (bloco + "\n\n")
        else:
            if parte_atual.strip():
                requests.post(
                    url,
                    json={
                        "chat_id": chat_id,
                        "text": parte_atual.strip(),
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                    },
                    timeout=20,
                )
            parte_atual = bloco + "\n\n"

    if parte_atual.strip():
        resp = requests.post(
            url,
            json={
                "chat_id": chat_id,
                "text": parte_atual.strip(),
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
            },
            timeout=20,
        )
        if not resp.ok:
            print(f"[!] Erro Telegram texto: {resp.text}")


# ==============================================================================
# COLETA DE ESTATÍSTICAS HISTÓRICAS E ODDS DO DIA
# ==============================================================================

def carregar_estatisticas_liga(url_csv: str, formato_novo: bool) -> Dict[str, dict]:
    try:
        df = pd.read_csv(url_csv)
    except Exception as e:
        print(f"   [!] Não foi possível ler CSV histórico ({url_csv}): {e}")
        return {}

    if formato_novo:
        col_h, col_a, col_hg, col_ag = "Home", "Away", "HG", "AG"
        df = df.tail(450)
    else:
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
            "fora_gols_pro": round(jogos_fora[col_ag].mean(), 2),
            "fora_gols_sofridos": round(jogos_fora[col_hg].mean(), 2),
            "fora_over25_%": round(jogos_fora["over25"].mean() * 100, 1),
            "fora_btts_%": round(jogos_fora["btts"].mean() * 100, 1),
        }

    return stats_times


def buscar_odds_liga(api_key: str, odds_key: str) -> List[dict]:
    url = f"https://api.the-odds-api.com/v4/sports/{odds_key}/odds"
    agora_utc = datetime.now(timezone.utc)
    limite_utc = agora_utc + timedelta(hours=30)

    params = {
        "apiKey": api_key,
        "regions": "eu,uk,us",
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
    for k, v in base_stats.items():
        if k in norm or norm in k:
            return v
    return None


def analisar_oportunidades_dia(odds_api_key: str) -> pd.DataFrame:
    registros = []
    base_global_stats: Dict[str, dict] = {}

    for liga in LIGAS_ALVO:
        print(f"\n[Analisando] {liga['nome']}...")
        eventos = buscar_odds_liga(odds_api_key, liga["odds_key"])
        if not eventos:
            print("   -> Nenhum jogo nas próximas 30h.")
            continue

        if liga["csv_historico"]:
            stats_liga = carregar_estatisticas_liga(liga["csv_historico"], liga["formato_novo"])
            if not liga["formato_novo"]:
                base_global_stats.update(stats_liga)
        else:
            if not base_global_stats:
                for l_dom in LIGAS_ALVO:
                    if l_dom["csv_historico"] and not l_dom["formato_novo"]:
                        base_global_stats.update(
                            carregar_estatisticas_liga(l_dom["csv_historico"], l_dom["formato_novo"])
                        )
            stats_liga = base_global_stats

        print(f"   -> {len(eventos)} jogo(s) encontrado(s) | {len(stats_liga)} times na base.")

        for ev in eventos:
            home = ev["home_team"]
            away = ev["away_team"]
            horario_utc = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
            horario_brt = (horario_utc - timedelta(hours=3)).strftime("%d/%m %H:%M")

            odds = extrair_melhores_odds_evento(ev)
            st_home = encontrar_stats_time(home, stats_liga)
            st_away = encontrar_stats_time(away, stats_liga)

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
# SÍNTESE COM GEMINI E DISPARO EM TEXTO PURO NO TELEGRAM
# ==============================================================================

def gerar_resumo_gemini(client: genai.Client, df_jogos: pd.DataFrame) -> str:
    tabela_txt = df_jogos.to_string(index=False)
    prompt = f"""
    Você é um analista quantitativo de apostas esportivas.
    Analise esta grade de jogos do dia com suas odds e métricas estatísticas:
    
    {tabela_txt}
    
    Escreva uma leitura rápida e objetiva em português (máximo 10 linhas, usando apenas tags HTML <b> e <i> para Telegram)
    destacando os jogos mais interessantes do dia (seja para Over 2.5, Ambas Marcam ou favoritismo com valor) com base nos números.
    Direto ao ponto, sem saudações ou introduções genéricas.
    """
    try:
        resp = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
        return resp.text.strip()
    except Exception as e:
        print(f"[!] Erro ao gerar comentário Gemini: {e}")
        return ""


def formatar_odd(val) -> str:
    return f"@{val:.2f}" if pd.notna(val) else "-"


def formatar_pct(val) -> str:
    return f"{val:.0f}%" if pd.notna(val) else "-"


def main():
    odds_key = os.environ.get("ODDS_API_KEY")
    gemini_key = os.environ.get("GEMINI_API_KEY")
    tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
    tg_chat = os.environ.get("TELEGRAM_CHAT_ID")

    if not odds_key:
        raise EnvironmentError("Defina ODDS_API_KEY nos Secrets do GitHub.")

    df_jogos = analisar_oportunidades_dia(odds_key)
    data_hoje = datetime.now().strftime("%d/%m/%Y")

    if not tg_token or not tg_chat:
        print("[Aviso] Telegram não configurado.")
        return

    if df_jogos.empty:
        enviar_telegram_texto(
            tg_token,
            tg_chat,
            f"⚽ <b>SCANNER MATINAL DE FUTEBOL — {data_hoje}</b>\n\nSem jogos das ligas monitoradas nas próximas 30 horas.",
        )
        return

    df_jogos = df_jogos.sort_values(by=["edge_over25_%", "horario_brt"], ascending=[False, True], na_position="last")
    df_destaques = df_jogos[df_jogos["edge_over25_%"].notna() & (df_jogos["edge_over25_%"] >= 5.0)]

    blocos = [
        f"⚽ <b>SCANNER MATINAL DE FUTEBOL — {data_hoje}</b>\n<i>{len(df_jogos)} jogo(s) analisado(s) nas próximas 30h</i>"
    ]

    if not df_destaques.empty:
        dest_txt = ["🔥 <b>DESTAQUES COM VALOR (+EV ≥ 5%):</b>"]
        for _, r in df_destaques.iterrows():
            jogo = html.escape(str(r["jogo"]))
            dest_txt.append(
                f"• <b>{jogo}</b> ({r['horario_brt']})\n"
                f"  Over 2.5: <b>{formatar_odd(r['odd_over25'])}</b> | Hist: <b>{formatar_pct(r['prob_over25_est_%'])}</b> (Edge: <b>+{r['edge_over25_%']}%</b>)"
            )
        blocos.append("\n".join(dest_txt))

    for liga, grupo in df_jogos.groupby("liga"):
        liga_txt = [f"🏆 <b>{html.escape(str(liga))}</b>"]
        for _, r in grupo.iterrows():
            jogo = html.escape(str(r["jogo"]))
            edge_str = f"{r['edge_over25_%']:+.1f}%" if pd.notna(r["edge_over25_%"]) else "-"
            xg_str = f"{r['media_gols_projetada']:.2f}" if pd.notna(r["media_gols_projetada"]) else "-"

            liga_txt.append(
                f"⏰ <code>{r['horario_brt']}</code> — <b>{jogo}</b>\n"
                f"   1x2: <code>{formatar_odd(r['odd_casa'])}</code> | <code>{formatar_odd(r['odd_empate'])}</code> | <code>{formatar_odd(r['odd_fora'])}</code>\n"
                f"   Over 2.5: <b>{formatar_odd(r['odd_over25'])}</b> (Hist: {formatar_pct(r['prob_over25_est_%'])} | Edge: <b>{edge_str}</b>)\n"
                f"   BTTS Hist: <b>{formatar_pct(r['prob_btts_est_%'])}</b> | Gols Proj.: <b>{xg_str}</b>"
            )
        blocos.append("\n\n".join(liga_txt))

    if gemini_key:
        client = genai.Client(http_options={"api_version": "v1beta"})
        analise_ia = gerar_resumo_gemini(client, df_jogos.head(12))
        if analise_ia:
            blocos.append(f"🧠 <b>Leitura Rápida IA:</b>\n{analise_ia}")

    mensagem_final = "\n\n".join(blocos)
    enviar_telegram_texto(tg_token, tg_chat, mensagem_final)
    print("\n[Concluído] Grade completa enviada em texto direto no Telegram!")


if __name__ == "__main__":
    main()
