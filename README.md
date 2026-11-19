# ⚽ Central Quantitativa de Apostas (+EV Scanner & Gestão de Banca)

Pipeline automatizado em Python + GitHub Actions integrado ao Telegram, Gemini 2.5 Flash e Google Sheets para análise quantitativa pré-jogo e rastreamento de banca.

## ⚙️ Módulos

### 1. Scanner Matinal +EV (`scanner_futebol.py`)
- **Agendamento:** Todo dia às 08h00 (Horário de Brasília).
- **Ligas Monitoradas:** Brasileirão Série A, Premier League, La Liga, Serie A (Itália), Bundesliga, Ligue 1, UEFA Champions League e MLS.
- **Lógica Matemática:** Cruza estatísticas históricas de gols (Football-Data) com odds ao vivo (The Odds API) para calcular o **Edge** (`Probabilidade Estimada - Probabilidade Implícita`) em **Over 2.5** e **Under 2.5**, filtrando entradas com `Edge >= +2.0%` e gerando leitura tática via **Gemini 2.5 Flash**.

### 2. Rastreador de Banca & ROI (`rastreador_banca.py`) *(em implantação)*
- **Registro via Telegram:** Interpretação em linguagem natural das entradas enviadas no chat do bot.
- **Persistência:** Gravação estruturada no **Google Sheets** pronta para visualização no **Power BI**.

## 🔐 Secrets Necessários (GitHub Actions)
- `ODDS_API_KEY`: Chave da The Odds API.
- `GEMINI_API_KEY`: Chave da API do Google Gemini.
- `TELEGRAM_BOT_TOKEN`: Token do bot do Scanner.
- `TELEGRAM_CHAT_ID`: ID pessoal do chat no Telegram.
- `TELEGRAM_BANCA_TOKEN`: Token do bot de Gestão de Banca.
- `GSHEETS_ID`: ID da planilha no Google Sheets.
- `GOOGLE_CREDENTIALS_JSON`: JSON da Service Account do Google Cloud.
