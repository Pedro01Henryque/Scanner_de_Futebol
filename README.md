# ⚽ Central Quantitativa de Apostas (+EV Scanner & Gestão de Banca)

Pipeline automatizado em **Python + GitHub Actions** integrado ao **Telegram**, **Gemini 2.5 Flash** e **Google Sheets** para detecção diária de valor esperado positivo (+EV), extração multimodal de bilhetes (OCR via print) e gestão automatizada de banca e ROI.

---

## 🏗️ Arquitetura e Stack Tecnológica

* **Linguagem & Runtime:** Python 3.11
* **Orquestração & Agendamento:** GitHub Actions (execução serverless agendada via `cron`)
* **IA & Visão Multimodal:** Google Gemini 2.5 Flash (`google-genai` com Structured Outputs / Pydantic)
* **Persistência & BI:** Google Sheets API (`gspread` + Service Account) estruturada para consumo direto no **Power BI** e **Looker Studio**
* **Interface & Alertas:** Telegram Bot API

---

## ⚙️ Módulos em Produção

### 1. Scanner Matinal +EV (`scanner_futebol.py`)
* **Agendamento:** Diariamente às **08h00** (Horário de Brasília).
* **Ligas Monitoradas:** Brasileirão Série A, Premier League, La Liga, Serie A (Itália), Bundesliga, Ligue 1, UEFA Champions League e MLS.
* **Motor Quantitativo:** Cruza estatísticas históricas de gols (Football-Data) com cotações ao vivo (The Odds API) para calcular o **Edge** (`Probabilidade Estimada - Probabilidade Implícita`) nos mercados de **Over 2.5** e **Under 2.5**, filtrando oportunidades com `Edge >= +2.0%` e gerando análise tática contextualizada via **Gemini 2.5 Flash**.

### 2. Rastreador Multimodal de Banca & ROI (`rastreador_banca.py`)
* **Agendamento:** Verificação automática de entradas no Telegram a cada hora (minuto `:45`) + **Boletim Semanal de Performance** toda segunda-feira às **08h30** (Horário de Brasília).
* **Registro via Print ou Texto:** Leitura inteligente de capturas de tela de bilhetes (Stake, Betano, Bet365, etc.) ou mensagens em linguagem natural, identificando automaticamente apostas **simples e múltiplas**, calculando a odd composta e formatando cada seleção linha a linha.
* **Integridade de Planilha:** Gravação estrita nas colunas `A:F` (`Data`, `Casa`, `Esporte`, `Aposta`, `ODD`, `Valor Apostado` em formato numérico decimal) na primeira linha disponível da coluna `A`, preservando intactas as fórmulas nativas das colunas `G:J`.
* **Gestão de Ciclo de Vida:** Entradas registradas com status **`Aberto`** na coluna `H` e atualização rápida via comandos (`Green`, `Red`, `Void`) ou consulta instantânea de métricas via `/resumo`.

---

## 📁 Estrutura do Repositório

```text
├── .github/
│   └── workflows/
│       ├── monitor.yml          # Workflow do Scanner +EV diário
│       └── banca.yml            # Workflow do Rastreador de Banca e Boletim Semanal
├── scanner_futebol.py           # Motor de cálculo +EV e alertas pré-jogo
├── rastreador_banca.py          # Parser multimodal (OCR/Texto) e integração Google Sheets
├── requirements.txt             # Dependências do projeto
└── README.md                    # Documentação
