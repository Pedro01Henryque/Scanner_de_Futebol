import html
import os
import re
import unicodedata
from datetime import datetime
from io import BytesIO
from typing import List, Optional
from urllib.parse import urljoin

import pandas as pd
import requests
from PIL import Image
from pydantic import BaseModel, Field
from playwright.sync_api import sync_playwright
from google import genai
from google.genai import types

# ==============================================================================
# CONFIGURAÇÕES E SCHEMAS PYDANTIC
# ==============================================================================

GEMINI_MODEL = "gemini-2.5-flash"
ARQUIVO_REFERENCIA = "referencia_precos.csv"
ARQUIVO_SAIDA = f"ofertas_mogi_{datetime.now().strftime('%Y-%m-%d')}.xlsx"

URLS_ALVO = {
    "Comercial Esperança (Mogi)": "https://comercialesperanca.com.br/ofertas-semana",
    "Max Atacadista (Loja 119 Mogi)": "https://www.maxatacadista.com.br/lojas/encartes-e-videos/?loja=119#&gid=null&pid=1",
    "Semar (Loja 08 Cézar de Souza)": "https://semarentrega.com.br/loja-08-cezar-de-souza",
    "Shibata (Mogi César)": "https://shibata.com.br/ofertas/jornal-de-ofertas-cruzeiro-itaqua-mogi-cesar-poa-e-guara/",
}


class ProdutoOferta(BaseModel):
    produto: str = Field(description="Nome completo do produto, marca e corte/tipo")
    categoria: str = Field(
        description="Categoria: Açougue, Mercearia, Frios/Laticínios, Hortifrúti, Limpeza/Higiene ou Bebidas"
    )
    preco_promocional: float = Field(description="Preço promocional numérico em Reais (ex: 19.90)")
    unidade_medida: str = Field(
        description="Unidade ou peso da embalagem (ex: kg, 500g, 5kg, 1L, 900ml, un, fardo 16un)"
    )
    preco_normal_de: Optional[float] = Field(
        default=None, description="Preço original 'De' caso esteja explícito no anúncio"
    )
    observacao: Optional[str] = Field(
        default=None, description="Condição especial, ex: 'Clube', 'Atacado acima de 3un', 'Quarta da Carne'"
    )


class ListaOfertasEncarte(BaseModel):
    validade_encarte: Optional[str] = Field(
        default=None, description="Período de validade escrito no encarte, se visível"
    )
    itens: List[ProdutoOferta]


# ==============================================================================
# FUNÇÕES DE ENVIO PARA O TELEGRAM
# ==============================================================================

def enviar_telegram_texto(token: str, chat_id: str, mensagem_html: str) -> None:
    """Envia mensagens formatadas em HTML para o Telegram, dividindo se passar de 4000 chars."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    limite = 4000
    partes = [mensagem_html[i : i + limite] for i in range(0, len(mensagem_html), limite)]

    for parte in partes:
        payload = {
            "chat_id": chat_id,
            "text": parte,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        resp = requests.post(url, json=payload, timeout=20)
        if not resp.ok:
            print(f"   [!] Erro ao enviar mensagem no Telegram: {resp.text}")


def enviar_telegram_arquivo(token: str, chat_id: str, caminho_arquivo: str, legenda: str) -> None:
    """Envia a planilha Excel gerada como documento no Telegram."""
    url = f"https://api.telegram.org/bot{token}/sendDocument"
    with open(caminho_arquivo, "rb") as f:
        resp = requests.post(
            url,
            data={"chat_id": chat_id, "caption": legenda},
            files={"document": f},
            timeout=30,
        )
    if not resp.ok:
        print(f"   [!] Erro ao enviar arquivo no Telegram: {resp.text}")


def notificar_ofertas_telegram(df_melhores: pd.DataFrame, df_final: pd.DataFrame, caminho_excel: str) -> None:
    """Monta o boletim matinal das melhores ofertas e dispara no Telegram."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        print("   [Aviso] TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID não configurados. Pulando envio.")
        return

    data_hoje = datetime.now().strftime("%d/%m/%Y")

    if df_melhores.empty:
        msg = (
            f"🛒 <b>Monitor de Ofertas Mogi ({data_hoje})</b>\n\n"
            f"Varri {len(df_final)} itens hoje, mas nenhum ficou pelo menos 5% abaixo da média cadastrada.\n"
            f"Segue a planilha completa em anexo para conferência."
        )
        enviar_telegram_texto(token, chat_id, msg)
        enviar_telegram_arquivo(token, chat_id, caminho_excel, f"Planilha completa - {data_hoje}")
        return

    linhas = [
        f"🛒 <b>MELHORES OFERTAS DO DIA - MOGI ({data_hoje})</b>",
        f"<i>{len(df_melhores)} itens abaixo da média regional encontrados:</i>\n",
    ]

    for mercado, grupo in df_melhores.groupby("mercado"):
        linhas.append(f"🏪 <b>{html.escape(str(mercado))}</b>")
        for _, row in grupo.iterrows():
            prod = html.escape(str(row["produto"]))
            preco = f"R$ {row['preco_promocional']:.2f}".replace(".", ",")
            media = f"R$ {row['preco_medio_mogi']:.2f}".replace(".", ",")
            desc = f"{row['desconto_vs_media_%']:.0f}%"
            obs = f" ({html.escape(str(row['observacao']))})" if pd.notna(row["observacao"]) and row["observacao"] else ""

            linhas.append(f"• <b>{prod}</b>{obs}: <b>{preco}</b> <i>(média {media} | -{desc})</i>")
        linhas.append("")

    mensagem_completa = "\n".join(linhas)
    enviar_telegram_texto(token, chat_id, mensagem_completa)
    enviar_telegram_arquivo(
        token,
        chat_id,
        caminho_excel,
        f"📊 Planilha completa ({len(df_final)} ofertas) - {data_hoje}",
    )
    print("   -> Resumo e planilha enviados com sucesso para o Telegram!")


# ==============================================================================
# FUNÇÕES DE NORMALIZAÇÃO E VISÃO COMPUTACIONAL (GEMINI)
# ==============================================================================

def normalizar_texto(texto: str) -> str:
    nfkd = unicodedata.normalize("NFKD", str(texto))
    sem_acento = "".join([c for c in nfkd if not unicodedata.combining(c)])
    return re.sub(r"\s+", " ", sem_acento.lower()).strip()


def baixar_imagem_pil(url: str) -> Optional[Image.Image]:
    try:
        headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
        resp = requests.get(url, headers=headers, timeout=20)
        resp.raise_for_status()
        img = Image.open(BytesIO(resp.content))
        if img.width < 450 or img.height < 450:
            return None
        return img.convert("RGB")
    except Exception as e:
        print(f"   [!] Erro ao baixar imagem {url}: {e}")
        return None


def extrair_ofertas_de_imagens_gemini(
    client: genai.Client, imagens: List[Image.Image], nome_mercado: str
) -> List[dict]:
    if not imagens:
        return []

    prompt = f"""
    Você é um analista de preços de supermercado especializado em leitura de encartes brasileiros.
    Analise estas imagens do encarte do mercado '{nome_mercado}' em Mogi das Cruzes - SP.
    Extraia TODOS os produtos e preços promocionais visíveis nas lâminas.
    
    Regras estritas:
    1. Inclua marca e peso/volume no nome do produto sempre que visível (ex: 'Arroz Tipo 1 Camil 5kg').
    2. Se houver preço 'Clube' / 'App' / 'Atacado' e preço normal, use o menor preço promocional e anote a condição no campo 'observacao'.
    3. Converta todos os preços para float com ponto decimal (ex: R$ 19,99 -> 19.99).
    4. Não invente itens que não estejam claramente legíveis na imagem.
    """

    resultados = []
    for i in range(0, len(imagens), 3):
        lote = imagens[i : i + 3]
        print(f"   -> Enviando lote de {len(lote)} lâmina(s) de '{nome_mercado}' para o Gemini...")
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=[*lote, prompt],
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=ListaOfertasEncarte,
                    temperature=0.1,
                ),
            )
            dados = ListaOfertasEncarte.model_validate_json(response.text)
            for item in dados.itens:
                item_dict = item.model_dump()
                item_dict["mercado"] = nome_mercado
                item_dict["validade"] = dados.validade_encarte or "Verificar encarte"
                resultados.append(item_dict)
        except Exception as e:
            print(f"   [!] Falha na extração Gemini para {nome_mercado}: {e}")

    return resultados


# ==============================================================================
# COLETORES ESPECÍFICOS POR SUPERMERCADO
# ==============================================================================

def coletar_imagens_encartes(page, mercado: str, url: str) -> List[Image.Image]:
    print(f"\n[Coletando] {mercado}...")
    imagens_validas = []
    urls_coletadas = set()

    try:
        # domcontentloaded evita timeout causado por scripts de rastreamento/analytics
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)

        if "comercialesperanca" in url:
            selects = page.locator("select").all()
            for sel in selects:
                if "Mogi" in sel.inner_text():
                    try:
                        sel.select_option(label="Mogi das Cruzes")
                        page.wait_for_timeout(3000)
                    except Exception:
                        pass

        for _ in range(5):
            page.mouse.wheel(0, 1200)
            page.wait_for_timeout(1000)

        if "maxatacadista" in url:
            for _ in range(12):
                for img_el in page.locator(".pswp__img").all():
                    src = img_el.get_attribute("src")
                    if src and src not in urls_coletadas and "blank" not in src:
                        urls_coletadas.add(urljoin(url, src))
                btn_next = page.locator(".pswp__button--arrow--right")
                if btn_next.is_visible():
                    btn_next.click()
                    page.wait_for_timeout(1200)
                else:
                    break

        for img_el in page.locator("img").all():
            src = img_el.get_attribute("src") or img_el.get_attribute("data-src")
            if not src:
                continue
            src_full = urljoin(url, src)
            ignorar = ["logo", "icon", "svg", "avatar", "whatsapp", "facebook", "instagram", "banner-topo"]
            if any(k in src_full.lower() for k in ignorar):
                continue
            urls_coletadas.add(src_full)

        for img_url in list(urls_coletadas)[:15]:
            img_pil = baixar_imagem_pil(img_url)
            if img_pil:
                imagens_validas.append(img_pil)

        print(f"   -> {len(imagens_validas)} lâmina(s) de alta resolução prontas para leitura.")
    except Exception as e:
        print(f"   [!] Erro ao navegar em {mercado}: {e}")

    return imagens_validas


def coletar_semar_ecommerce(page, client: genai.Client) -> List[dict]:
    mercado = "Semar (Loja 08 Cézar de Souza)"
    url = URLS_ALVO[mercado]
    print(f"\n[Coletando] {mercado}...")
    ofertas_semar = []

    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)

        page.goto("https://semarentrega.com.br/loja-08-cezar-de-souza/ofertas", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)

        for _ in range(6):
            page.mouse.wheel(0, 1200)
            page.wait_for_timeout(1000)

        texto_pagina = page.locator("body").inner_text()
        linhas_relevantes = [l.strip() for l in texto_pagina.split("\n") if len(l.strip()) > 2]
        bloco_texto = "\n".join(linhas_relevantes[:450])

        prompt = f"""
        Extraia os produtos em oferta listados neste texto raspado do e-commerce '{mercado}'.
        Identifique nome do produto, peso/unidade, preço promocional ('Por') e preço antigo ('De', se houver).
        Ignore menus de navegação, rodapés e categorias vazias.
        
        TEXTO DA PÁGINA:
        {bloco_texto}
        """

        response = client.models.generate_content(
            model=GEMINI_MODEL,
            contents=prompt,
            config=types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=ListaOfertasEncarte,
                temperature=0.1,
            ),
        )
        dados = ListaOfertasEncarte.model_validate_json(response.text)
        for item in dados.itens:
            item_dict = item.model_dump()
            item_dict["mercado"] = mercado
            item_dict["validade"] = "Oferta do dia (Site)"
            ofertas_semar.append(item_dict)

        print(f"   -> {len(ofertas_semar)} ofertas extraídas do catálogo Semar.")
    except Exception as e:
        print(f"   [!] Erro ao coletar Semar: {e}")

    return ofertas_semar


# ==============================================================================
# CRUZAMENTO COM PREÇOS MÉDIOS E EXECUÇÃO PRINCIPAL
# ==============================================================================

def comparar_com_media_regional(df_ofertas: pd.DataFrame, caminho_ref: str) -> pd.DataFrame:
    if df_ofertas.empty:
        return df_ofertas

    df_ref = pd.read_csv(caminho_ref)
    referencias = [
        {
            "nome_ref": row["produto_referencia"],
            "kws": [normalizar_texto(k) for k in str(row["palavras_chave"]).split(",")],
            "preco_medio": float(row["preco_medio_mogi"]),
        }
        for _, row in df_ref.iterrows()
    ]

    matches_nome, matches_preco_medio, descontos_pct, economias_rs, status_oferta = [], [], [], [], []

    for _, item in df_ofertas.iterrows():
        nome_norm = normalizar_texto(item["produto"])
        preco_atual = float(item["preco_promocional"])

        ref_encontrada = next((r for r in referencias if any(kw in nome_norm for kw in r["kws"])), None)

        if ref_encontrada:
            media = ref_encontrada["preco_medio"]
            economia = round(media - preco_atual, 2)
            pct = round((economia / media) * 100, 1)

            matches_nome.append(ref_encontrada["nome_ref"])
            matches_preco_medio.append(media)
            economias_rs.append(economia)
            descontos_pct.append(pct)

            if pct >= 15.0:
                status_oferta.append("EXCELENTE (≥15% abaixo da média)")
            elif pct >= 5.0:
                status_oferta.append("BOA OFERTA (5% a 14% abaixo)")
            elif pct >= -5.0:
                status_oferta.append("NA MÉDIA DA REGIÃO")
            else:
                status_oferta.append("ACIMA DA MÉDIA")
        else:
            matches_nome.append("Sem referência cadastrada")
            matches_preco_medio.append(None)
            economias_rs.append(None)
            descontos_pct.append(None)
            status_oferta.append("Novo item / Avaliar manual")

    df_ofertas["item_referencia"] = matches_nome
    df_ofertas["preco_medio_mogi"] = matches_preco_medio
    df_ofertas["economia_reais"] = economias_rs
    df_ofertas["desconto_vs_media_%"] = descontos_pct
    df_ofertas["avaliacao"] = status_oferta

    return df_ofertas


def main():
    if not os.environ.get("GEMINI_API_KEY"):
        raise EnvironmentError("Defina a variável de ambiente GEMINI_API_KEY.")

    # Força v1beta para habilitar JSON mode / Structured Outputs sem erro 400
    client = genai.Client(http_options={"api_version": "v1beta"})
    todas_ofertas = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            viewport={"width": 1366, "height": 900},
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        )
        page = context.new_page()

        for mercado in [
            "Comercial Esperança (Mogi)",
            "Max Atacadista (Loja 119 Mogi)",
            "Shibata (Mogi César)",
        ]:
            imgs = coletar_imagens_encartes(page, mercado, URLS_ALVO[mercado])
            ofertas = extrair_ofertas_de_imagens_gemini(client, imgs, mercado)
            todas_ofertas.extend(ofertas)

        ofertas_semar = coletar_semar_ecommerce(page, client)
        todas_ofertas.extend(ofertas_semar)

        browser.close()

    if not todas_ofertas:
        print("\n[Aviso] Nenhuma oferta extraída hoje.")
        token = os.environ.get("TELEGRAM_BOT_TOKEN")
        chat_id = os.environ.get("TELEGRAM_CHAT_ID")
        if token and chat_id:
            enviar_telegram_texto(token, chat_id, "⚠️ <b>Monitor Mogi:</b> Nenhuma oferta pôde ser extraída hoje. Verifique os logs no GitHub.")
        return

    df_bruto = pd.DataFrame(todas_ofertas)
    df_final = comparar_com_media_regional(df_bruto, ARQUIVO_REFERENCIA)

    df_final = df_final.sort_values(
        by=["desconto_vs_media_%", "preco_promocional"],
        ascending=[False, True],
        na_position="last",
    )

    df_melhores = df_final[
        df_final["desconto_vs_media_%"].notna() & (df_final["desconto_vs_media_%"] >= 5.0)
    ]

    with pd.ExcelWriter(ARQUIVO_SAIDA, engine="openpyxl") as writer:
        df_melhores.to_excel(writer, sheet_name="Melhores Ofertas Hoje", index=False)
        df_final.to_excel(writer, sheet_name="Todas Ofertas Extraidas", index=False)

    print(f"\n[Concluído] {len(df_final)} ofertas analisadas | {len(df_melhores)} em destaque.")
    notificar_ofertas_telegram(df_melhores, df_final, ARQUIVO_SAIDA)


if __name__ == "__main__":
    main()
