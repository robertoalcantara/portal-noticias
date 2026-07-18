#!/usr/bin/env python3
"""
Gera matérias para o BRGrid a partir de várias fontes (RSS e sites sem feed).

Fluxo:
  1. Lê a lista de fontes em sources.yaml (RSS ou listagem HTML)
  2. Junta todas as manchetes novas (não vistas antes) de todas as fontes
  3. Pede ao Claude para AGRUPAR manchetes que tratam do mesmo fato/evento
     (mesmo vindas de fontes diferentes) e classificar cada grupo numa das
     categorias permitidas — grupos fora do escopo são descartados
  4. Para cada grupo válido, baixa o texto completo de cada fonte do grupo
     e pede ao Claude (modelo rápido) para produzir UMA matéria original em
     português, agregando os fatos de todas as fontes do grupo
  5. Passa o texto por uma segunda revisão com um modelo mais forte, que
     compara cada afirmação contra os textos-fonte e corrige imprecisões
  6. Grava um arquivo Markdown em site/content/posts/ (Hugo publica sozinho)

O script é resiliente: erro num grupo não derruba a execução inteira.
Rode localmente com `python pipeline/generate.py` ou deixe o GitHub Actions rodar.

Variáveis de ambiente:
  ANTHROPIC_API_KEY  (obrigatória, exceto em DRY_RUN)
  MODEL              (opcional, padrão: claude-haiku-4-5-20251001 — geração)
  FACTCHECK_MODEL    (opcional, padrão: claude-sonnet-5 — revisão/checagem)
  CLUSTER_MODEL      (opcional, padrão: claude-haiku-4-5-20251001 — agrupar/classificar)
  MAX_PER_FEED       (opcional, padrão: 4 — manchetes novas por fonte/rodada)
  MAX_SOURCE_CHARS   (opcional, padrão: 6000 — corte de CADA texto-fonte)
  DRY_RUN            (opcional, "1" para não chamar a API — usa texto de teste)
  UNSPLASH_ACCESS_KEY (opcional — banco de fotos principal)
  PEXELS_API_KEY     (opcional — reserva, tentado se o Unsplash não achar
                       nada ou não tiver chave configurada)
  GEMINI_API_KEY      (opcional — se configurada, tenta gerar uma variação por
                       IA da imagem que já existe na matéria-fonte, ANTES de
                       tentar o banco de fotos genérico)
  GEMINI_IMAGE_MODEL  (opcional, padrão: gemini-2.5-flash-image — modelo de
                       geração/edição de imagem "Nano Banana" do Gemini)
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import random
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

import feedparser
import trafilatura
import yaml
from slugify import slugify

ROOT = Path(__file__).resolve().parent.parent
SOURCES_FILE = ROOT / "pipeline" / "sources.yaml"
SEEN_FILE = ROOT / "pipeline" / "seen.json"
POSTS_DIR = ROOT / "site" / "content" / "posts"

MODEL = os.environ.get("MODEL", "claude-haiku-4-5-20251001")
FACTCHECK_MODEL = os.environ.get("FACTCHECK_MODEL", "claude-sonnet-5")
CLUSTER_MODEL = os.environ.get("CLUSTER_MODEL", "claude-haiku-4-5-20251001")
MAX_PER_FEED = int(os.environ.get("MAX_PER_FEED", "4"))
MAX_SOURCE_CHARS = int(os.environ.get("MAX_SOURCE_CHARS", "6000"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"
PEXELS_API_KEY = os.environ.get("PEXELS_API_KEY", "")
UNSPLASH_ACCESS_KEY = os.environ.get("UNSPLASH_ACCESS_KEY", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_IMAGE_MODEL = os.environ.get("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")

# Categorias que o portal cobre. Qualquer manchete fora disso é descartada
# na etapa de classificação (não é gerada matéria).
ALLOWED_CATEGORIES = ["Kart", "F1", "F2", "F3", "F4", "GT3", "WEC", "Indy", "NASCAR"]

# Termos de busca em inglês para o banco de fotos (Pexels não tem fotos dos
# eventos específicos das matérias, então buscamos algo genérico e coerente
# com a categoria).
CATEGORY_STOCK_QUERIES = {
    "Kart": "go kart racing track",
    "F1": "formula one race car track",
    "F2": "formula racing car track",
    "F3": "formula racing car track",
    "F4": "formula racing car track",
    "GT3": "GT race car track",
    "WEC": "endurance race car track night",
    "Indy": "indycar open wheel race",
    "NASCAR": "nascar stock car race",
}

CLUSTER_SYSTEM_PROMPT = f"""\
Você organiza a pauta de um portal de notícias de automobilismo chamado BRGrid.
Vai receber uma lista de manchetes (com id, fonte e resumo) vindas de vários
sites diferentes na mesma rodada de coleta.

Tarefas:
1. AGRUPE manchetes que tratam do MESMO fato/evento específico (mesma sessão,
   mesmo anúncio, mesmo resultado de corrida), mesmo vindas de fontes
   diferentes e com títulos diferentes. Manchetes sobre fatos diferentes
   (sessões diferentes, anúncios diferentes) NÃO devem ser agrupadas, mesmo
   que sejam da mesma categoria ou do mesmo piloto.
2. Toda manchete deve aparecer em exatamente um grupo (grupos podem ter só
   1 manchete, se não houver duplicata).
3. Para cada grupo, atribua uma categoria dentre exatamente estas:
   {", ".join(ALLOWED_CATEGORIES)}
   (Kart = kartismo em qualquer lugar do mundo; F1/F2/F3/F4 = categorias da
   pirâmide FIA de monopostos; GT3 = carros de turismo/GT; WEC = Mundial de
   Endurance; Indy = IndyCar; NASCAR = NASCAR.)
4. Se o assunto do grupo NÃO se encaixa em nenhuma dessas categorias (ex.:
   MotoGP, Rally, Fórmula E, Stock Car, notícia institucional sem relação
   com pista), marque a categoria desse grupo como "DESCARTAR".
5. Algumas manchetes vêm com uma "regra da fonte" anexada (uma instrução
   específica daquela fonte, não do site em geral). Se uma manchete violar
   a regra da fonte dela, NÃO a inclua em nenhum grupo — trate como se ela
   não existisse na lista, mesmo que o fato em si seja relevante para o
   escopo do site.

Responda APENAS com um objeto JSON válido (sem markdown, sem crases):
{{
  "grupos": [
    {{"ids": [0, 3], "categoria": "F1"}},
    {{"ids": [1], "categoria": "DESCARTAR"}}
  ]
}}"""

SYSTEM_PROMPT = f"""\
Você é um editor do BRGrid, portal brasileiro de notícias de automobilismo
focado em: {", ".join(ALLOWED_CATEGORIES)}.

Vai receber um ou mais textos-fonte (cada um com o nome da fonte) que tratam
do MESMO fato. Produza UMA matéria ORIGINAL em português do Brasil que
agregue os fatos de todas as fontes recebidas.

Regras obrigatórias:
- Escreva com suas próprias palavras. NÃO copie nem parafraseie frase a
  frase nenhum texto-fonte.
- Baseie-se apenas nos FATOS presentes nos textos-fonte. Não invente dados,
  números, aspas, nomes ou nacionalidades.
- Se houver mais de uma fonte, combine os fatos em uma narrativa única e
  coerente — não escreva "segundo a fonte A... segundo a fonte B...".
- Se o material for insuficiente, escreva uma nota curta em vez de
  preencher com suposições.
- Tom jornalístico, direto, sem sensacionalismo.
- A(s) fonte(s) será(ão) creditada(s) pelo sistema; você não precisa citá-las.

Responda APENAS com um objeto JSON válido (sem markdown, sem crases), no formato:
{{
  "titulo": "título curto e informativo",
  "linha_fina": "uma frase de resumo (o 'dek')",
  "categoria": "uma de: {", ".join(ALLOWED_CATEGORIES)}",
  "tags": ["até 4 tags curtas"],
  "corpo_markdown": "3 a 5 parágrafos em Markdown"
}}"""

FACTCHECK_SYSTEM_PROMPT = """\
Você é o revisor de fatos (fact-checker) do BRGrid, portal de automobilismo.
Vai receber um ou mais TEXTOS-FONTE originais (rotulados por fonte) e um
RASCUNHO em português gerado por outro editor a partir deles. Seu trabalho é
comparar o rascunho contra os textos-fonte FRASE A FRASE e corrigir qualquer
imprecisão.

Verifique com atenção especial:
- Nomes de pilotos, equipes, patrocinadores e circuitos
- Nacionalidades e afiliações (equipe, categoria, fabricante)
- Números: tempos, posições, datas, contagem de pontos, resultados
- Relações de causa e efeito (ex.: quem lidera o quê, quem superou quem)
- Qualquer detalhe que soe específico mas não apareça em nenhum texto-fonte

Regras:
- Se um dado do rascunho não está em nenhum texto-fonte e não pode ser
  inferido com segurança, REMOVA ou GENERALIZE a frase — nunca mantenha um
  dado não verificado.
- Corrija o dado errado quando os textos-fonte permitirem confirmar o correto.
- Não adicione fatos novos que não estavam no rascunho nem nos textos-fonte.
- Preserve o tom jornalístico e a fluidez; corrija o mínimo necessário para
  garantir precisão, sem reescrever o texto do zero.
- Se o rascunho já estiver correto, devolva-o sem alterações.

Responda APENAS com um objeto JSON válido (sem markdown, sem crases), EXATAMENTE
no mesmo formato do rascunho recebido:
{
  "titulo": "...",
  "linha_fina": "...",
  "categoria": "...",
  "tags": ["..."],
  "corpo_markdown": "..."
}"""


# --------------------------------------------------------------------------
# seen.json
# --------------------------------------------------------------------------

def load_seen() -> set[str]:
    if SEEN_FILE.exists():
        try:
            return set(json.loads(SEEN_FILE.read_text(encoding="utf-8")))
        except Exception:
            return set()
    return set()


def save_seen(seen: set[str]) -> None:
    SEEN_FILE.write_text(
        json.dumps(sorted(seen), ensure_ascii=False, indent=0), encoding="utf-8"
    )


# --------------------------------------------------------------------------
# Coleta de candidatos (RSS ou listagem HTML)
# --------------------------------------------------------------------------

def entry_date_from_struct(struct_time) -> datetime:
    if struct_time:
        return datetime(*struct_time[:6], tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def collect_rss_candidates(feed_cfg: dict, seen: set[str]) -> list[dict]:
    name, url = feed_cfg["name"], feed_cfg["url"]
    extra_instructions = feed_cfg.get("extra_instructions")
    try:
        parsed = feedparser.parse(url)
    except Exception as exc:  # noqa: BLE001
        print(f"  erro ao ler o feed: {exc}", file=sys.stderr)
        return []
    if parsed.bozo and not parsed.entries:
        print(f"  feed vazio ou inválido ({url})", file=sys.stderr)
        return []

    out = []
    for entry in parsed.entries:
        if len(out) >= MAX_PER_FEED:
            break
        link = entry.get("link")
        if not link or link in seen:
            continue
        title = entry.get("title", "Sem título")
        summary = entry.get("summary", "") or entry.get("description", "")
        date = entry_date_from_struct(
            entry.get("published_parsed") or entry.get("updated_parsed")
        )
        out.append(
            {
                "name": name,
                "extra_instructions": extra_instructions,
                "title": title,
                "link": link,
                "summary": re.sub(r"<[^>]+>", " ", summary or "").strip(),
                "date": date,
            }
        )
    return out


def collect_list_candidates(feed_cfg: dict, seen: set[str]) -> list[dict]:
    """Para sites sem RSS: lê a página de listagem e extrai links de matéria."""
    name = feed_cfg["name"]
    list_url = feed_cfg["list_url"]
    link_contains = feed_cfg["link_contains"]
    extra_instructions = feed_cfg.get("extra_instructions")

    try:
        downloaded = trafilatura.fetch_url(list_url)
    except Exception as exc:  # noqa: BLE001
        print(f"  erro ao baixar listagem ({exc})", file=sys.stderr)
        return []
    if not downloaded:
        print(f"  listagem vazia ({list_url})", file=sys.stderr)
        return []

    hrefs = re.findall(r'href="([^"]+)"', downloaded)
    links: list[str] = []
    seen_in_page = set()
    for href in hrefs:
        absolute = urljoin(list_url, href)
        if link_contains not in absolute:
            continue
        if absolute in seen_in_page:
            continue
        seen_in_page.add(absolute)
        links.append(absolute)

    out = []
    for link in links:
        if len(out) >= MAX_PER_FEED:
            break
        if link in seen:
            continue
        try:
            article_html = trafilatura.fetch_url(link)
            if not article_html:
                continue
            data = trafilatura.bare_extraction(article_html, with_metadata=True)
        except Exception as exc:  # noqa: BLE001
            print(f"    aviso: falha ao ler {link} ({exc})", file=sys.stderr)
            continue
        if not data:
            continue
        # trafilatura pode devolver um dict OU um objeto Document, dependendo
        # da versão/parâmetros — trata os dois casos.
        if isinstance(data, dict):
            text = data.get("text")
            title = data.get("title")
            raw_date = data.get("date")
        else:
            text = getattr(data, "text", None)
            title = getattr(data, "title", None)
            raw_date = getattr(data, "date", None)
        if not text or len(text) < 120:
            continue
        title = title or link
        date = datetime.now(timezone.utc)
        if raw_date:
            try:
                date = datetime.fromisoformat(str(raw_date)).replace(tzinfo=timezone.utc)
            except Exception:  # noqa: BLE001
                pass
        out.append(
            {
                "name": name,
                "extra_instructions": extra_instructions,
                "title": title,
                "link": link,
                "summary": text[:500],
                "date": date,
                "_full_text": text,
                "_source_html": article_html,
            }
        )
    return out


def collect_all_candidates(feeds: list[dict], seen: set[str]) -> list[dict]:
    candidates = []
    for feed_cfg in feeds:
        print(f"\n== {feed_cfg['name']} ==")
        if feed_cfg.get("type") == "list":
            found = collect_list_candidates(feed_cfg, seen)
        else:
            found = collect_rss_candidates(feed_cfg, seen)
        print(f"  {len(found)} manchete(s) nova(s)")
        candidates.extend(found)
    return candidates


# --------------------------------------------------------------------------
# Parsing de JSON tolerante
# --------------------------------------------------------------------------

def parse_model_json(raw: str) -> dict:
    """Extrai o JSON da resposta do modelo, tolerando crases ou texto extra."""
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end != -1:
        cleaned = cleaned[start : end + 1]
    return json.loads(cleaned, strict=False)


# --------------------------------------------------------------------------
# Chamadas ao Claude
# --------------------------------------------------------------------------

def cluster_and_classify(candidates: list[dict]) -> list[dict]:
    """Agrupa candidatos pelo mesmo fato e classifica cada grupo."""
    if DRY_RUN:
        return [{"ids": [i], "categoria": ALLOWED_CATEGORIES[0]} for i in range(len(candidates))]

    from anthropic import Anthropic

    lines = []
    for i, c in enumerate(candidates):
        summary = (c.get("summary") or "")[:220].replace("\n", " ")
        line = f"id {i} | fonte: {c['name']} | título: {c['title']} | resumo: {summary}"
        if c.get("extra_instructions"):
            line += f" | regra da fonte: {c['extra_instructions']}"
        lines.append(line)
    user_content = "\n".join(lines)

    client = Anthropic()
    message = client.messages.create(
        model=CLUSTER_MODEL,
        max_tokens=4096,
        system=CLUSTER_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    raw = "".join(block.text for block in message.content if block.type == "text")
    try:
        data = parse_model_json(raw)
    except Exception as exc:  # noqa: BLE001
        print(f"    aviso: falha ao parsear resposta do agrupamento: {exc}", file=sys.stderr)
        print(f"    resposta bruta ({len(raw)} chars): {raw[:500]!r}", file=sys.stderr)
        print(f"    stop_reason: {getattr(message, 'stop_reason', '?')}", file=sys.stderr)
        raise
    return data.get("grupos", [])


def rewrite_with_claude(source_blocks: list[tuple[str, str, str | None]]) -> dict:
    """source_blocks: lista de (nome_da_fonte, texto, instrução_extra). Gera UMA matéria."""
    if DRY_RUN:
        names = ", ".join(n for n, _, _ in source_blocks)
        return {
            "titulo": f"[TESTE] Matéria de {names}",
            "linha_fina": "Matéria de teste gerada em modo DRY_RUN.",
            "categoria": ALLOWED_CATEGORIES[0],
            "tags": ["teste"],
            "corpo_markdown": (
                "Este é um texto de teste gerado sem chamar a API.\n\n"
                "Defina `DRY_RUN=0` e a variável `ANTHROPIC_API_KEY` para gerar de verdade."
            ),
        }

    from anthropic import Anthropic

    client = Anthropic()
    parts = []
    for name, text, extra_instructions in source_blocks:
        header = f"[Fonte: {name}]"
        if extra_instructions:
            header += f" [Instrução especial para esta fonte: {extra_instructions}]"
        parts.append(f"{header}\n{text[:MAX_SOURCE_CHARS]}")
    user_content = "\n\n---\n\n".join(parts)

    message = client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    raw = "".join(block.text for block in message.content if block.type == "text")
    return parse_model_json(raw)


def factcheck_with_claude(article: dict, source_blocks: list[tuple[str, str, str | None]]) -> dict:
    """Segunda passada: revisa o rascunho contra os textos-fonte originais."""
    if DRY_RUN:
        return article

    from anthropic import Anthropic

    client = Anthropic()
    parts = []
    for name, text, extra_instructions in source_blocks:
        header = f"[Fonte: {name}]"
        if extra_instructions:
            header += f" [Instrução especial para esta fonte: {extra_instructions}]"
        parts.append(f"{header}\n{text[:MAX_SOURCE_CHARS]}")
    sources_text = "\n\n---\n\n".join(parts)
    user_content = (
        f"TEXTOS-FONTE:\n{sources_text}\n\n"
        f"RASCUNHO (JSON):\n{json.dumps(article, ensure_ascii=False)}"
    )
    message = client.messages.create(
        model=FACTCHECK_MODEL,
        max_tokens=4096,
        system=FACTCHECK_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    raw = "".join(block.text for block in message.content if block.type == "text")
    return parse_model_json(raw)


# --------------------------------------------------------------------------
# Imagens: variação por IA (Gemini "Nano Banana") a partir da imagem-fonte
# --------------------------------------------------------------------------
#
# Antes de cair no banco de fotos genérico (abaixo), tentamos usar a própria
# imagem que ilustra a matéria no site de origem: baixamos essa imagem e
# pedimos a um modelo de geração/edição de imagem (Gemini, apelidado de
# "Nano Banana") para criar uma VARIAÇÃO dela — muda um pouco o ângulo das
# pessoas e dos carros visíveis, mas preserva o contexto geral da cena. A
# ideia é ter uma imagem com relação real ao fato noticiado (diferente do
# banco de fotos, que é sempre genérico por categoria) sem republicar a
# foto original de terceiros sem licença.
#
# Falha em qualquer etapa (matéria-fonte sem imagem, GEMINI_API_KEY não
# configurada, erro de rede/API) é silenciosa: a função devolve None e o
# chamador cai para o banco de fotos (get_article_image), exatamente como o
# Pexels é a reserva do Unsplash logo abaixo.

GENERATED_IMAGES_DIR = ROOT / "site" / "static" / "images" / "ia"
GENERATED_IMAGES_URL_PREFIX = "/images/ia"

# Prompt fixo pedido pelo dono do projeto para a variação de imagem.
IMAGE_VARIATION_PROMPT = (
    "Crie uma variação dessa imagem, mudando um pouco o ângulo das pessoas "
    "e dos carros visíveis, de forma pronunciada mas que não altere o "
    "contexto geral."
)


def extract_source_image_url(html: str, page_url: str) -> str | None:
    """Extrai a imagem principal (og:image/twitter:image) da página da
    matéria-fonte usando os metadados que o trafilatura já sabe ler."""
    if not html:
        return None
    try:
        meta = trafilatura.extract_metadata(html, default_url=page_url)
    except Exception as exc:  # noqa: BLE001
        print(f"    aviso: falha ao ler metadata da página-fonte ({exc})", file=sys.stderr)
        return None
    if not meta:
        return None
    # extract_metadata() normalmente devolve um Document, mas tratamos dict
    # também pelo mesmo motivo do bare_extraction() acima: já causou crash
    # em produção assumir um formato só.
    image = meta.get("image") if isinstance(meta, dict) else getattr(meta, "image", None)
    if not image:
        return None
    return urljoin(page_url, image)


def download_image_bytes(url: str, max_bytes: int = 8_000_000) -> tuple[bytes, str] | None:
    """Baixa uma imagem e devolve (bytes, mime_type). None se falhar, vier
    vazia/pequena demais (provável placeholder) ou grande demais."""
    req = urllib.request.Request(
        url, headers={"User-Agent": "Mozilla/5.0 (compatible; BRGridBot/1.0)"}
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            content_type = (resp.headers.get("Content-Type") or "image/jpeg").split(";")[0].strip()
            data = resp.read(max_bytes + 1)
    except Exception as exc:  # noqa: BLE001
        print(f"    aviso: falha ao baixar imagem da fonte ({exc})", file=sys.stderr)
        return None
    if not data or len(data) < 500 or len(data) > max_bytes:
        return None
    if not content_type.startswith("image/"):
        return None
    return data, content_type


def generate_image_variation(image_bytes: bytes, mime_type: str) -> tuple[bytes, str] | None:
    """Chama a API do Gemini (Nano Banana) para gerar uma variação da
    imagem recebida. Devolve (bytes_da_imagem_gerada, mime_type) ou None."""
    if not GEMINI_API_KEY:
        return None
    url = (
        "https://generativelanguage.googleapis.com/v1/models/"
        f"{GEMINI_IMAGE_MODEL}:generateContent"
    )
    # Sem "generationConfig"/"responseModalities" de propósito: não faz parte
    # do exemplo oficial de edição de imagem da API (generateContent) e causa
    # HTTP 400 nesse modelo — o formato abaixo é o documentado.
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": IMAGE_VARIATION_PROMPT},
                    {
                        "inline_data": {
                            "mime_type": mime_type,
                            "data": base64.b64encode(image_bytes).decode("ascii"),
                        }
                    },
                ]
            }
        ],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "x-goog-api-key": GEMINI_API_KEY},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        # O corpo do erro (JSON com "error.message") é o que realmente diz o
        # que houve de errado — sem isso, só se vê "HTTP Error 400: Bad
        # Request", que não ajuda em nada a depurar.
        try:
            body = exc.read().decode("utf-8", errors="replace")[:500]
        except Exception:  # noqa: BLE001
            body = "(sem corpo)"
        print(f"    aviso: falha ao chamar a API de imagem do Gemini (HTTP {exc.code}: {body})", file=sys.stderr)
        return None
    except Exception as exc:  # noqa: BLE001
        print(f"    aviso: falha ao chamar a API de imagem do Gemini ({exc})", file=sys.stderr)
        return None
    try:
        for candidate in data.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    out_mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
                    return base64.b64decode(inline["data"]), out_mime
    except Exception as exc:  # noqa: BLE001
        print(f"    aviso: resposta inesperada da API de imagem do Gemini ({exc})", file=sys.stderr)
    return None


def get_ai_variation_image(group_candidates: list[dict]) -> dict | None:
    """Tenta, na ordem das fontes do grupo, gerar uma variação por IA da
    imagem já usada na matéria-fonte. Devolve um dict no mesmo formato de
    get_article_image() ({"url", "credit_name", "credit_url", "provider"})
    ou None se nenhuma fonte do grupo render uma imagem utilizável — aí o
    chamador cai para o banco de fotos genérico."""
    if DRY_RUN or not GEMINI_API_KEY:
        return None

    for c in group_candidates:
        link = c.get("link")
        if not link:
            continue
        html = c.get("_source_html")
        if html is None:
            html = trafilatura.fetch_url(link)
        if not html:
            continue
        image_url = extract_source_image_url(html, link)
        if not image_url:
            continue
        downloaded = download_image_bytes(image_url)
        if not downloaded:
            continue
        image_bytes, mime_type = downloaded
        variation = generate_image_variation(image_bytes, mime_type)
        if not variation:
            continue
        variation_bytes, out_mime = variation
        ext = "png" if "png" in out_mime else "jpg"
        digest = hashlib.sha1(link.encode("utf-8")).hexdigest()[:16]
        filename = f"{digest}.{ext}"
        GENERATED_IMAGES_DIR.mkdir(parents=True, exist_ok=True)
        (GENERATED_IMAGES_DIR / filename).write_bytes(variation_bytes)
        return {
            "url": f"{GENERATED_IMAGES_URL_PREFIX}/{filename}",
            "credit_name": "",
            "credit_url": "",
            "provider": "IA (variação da imagem da fonte)",
        }
    return None


# --------------------------------------------------------------------------
# Imagens: banco de fotos (Unsplash, com Pexels como reserva) — usado só se
# a variação por IA acima não gerar nada
# --------------------------------------------------------------------------
#
# Cada provedor devolve um dict {"url", "credit_name", "credit_url",
# "provider"} ou None. O Unsplash exige, pelas regras da API dele:
#   1) hotlink direto (photo.urls.*, nunca rehost) — já fazemos isso
#   2) atribuição visível (fotógrafo + Unsplash, com link pro perfil)
#   3) avisar o Unsplash quando a foto é "usada" (endpoint download_location)
# O Pexels não exige nada disso, então devolve credit_name/credit_url vazios.

def trigger_unsplash_download(download_location: str) -> None:
    """Avisa o Unsplash que a foto foi usada (exigido pelas regras da API
    deles). Não é crítico — se falhar, só loga e segue o pipeline."""
    if not download_location:
        return
    req = urllib.request.Request(
        download_location, headers={"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"}
    )
    try:
        urllib.request.urlopen(req, timeout=10)
    except Exception as exc:  # noqa: BLE001
        print(f"    aviso: falha ao avisar o Unsplash sobre o uso da foto ({exc})", file=sys.stderr)


def fetch_unsplash_image(categoria: str) -> dict | None:
    if not UNSPLASH_ACCESS_KEY:
        return None
    query = CATEGORY_STOCK_QUERIES.get(categoria, "motorsport racing")
    url = (
        "https://api.unsplash.com/search/photos?"
        + urllib.parse.urlencode({"query": query, "per_page": 15, "orientation": "landscape"})
    )
    req = urllib.request.Request(url, headers={"Authorization": f"Client-ID {UNSPLASH_ACCESS_KEY}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"    aviso: falha ao buscar foto no Unsplash ({exc})", file=sys.stderr)
        return None
    results = data.get("results") or []
    if not results:
        return None
    photo = random.choice(results)
    image_url = photo.get("urls", {}).get("regular") or photo.get("urls", {}).get("full")
    if not image_url:
        return None

    user = photo.get("user", {})
    credit_name = user.get("name", "")
    credit_url = user.get("links", {}).get("html", "")
    if credit_url:
        sep = "&" if "?" in credit_url else "?"
        credit_url = f"{credit_url}{sep}utm_source=BRGrid&utm_medium=referral"

    download_location = photo.get("links", {}).get("download_location", "")
    trigger_unsplash_download(download_location)

    return {"url": image_url, "credit_name": credit_name, "credit_url": credit_url, "provider": "Unsplash"}


def fetch_pexels_image(categoria: str) -> dict | None:
    """Reserva: só é tentado se o Unsplash não achar nada (ou não tiver
    chave configurada). Pexels não exige atribuição."""
    if not PEXELS_API_KEY:
        return None
    query = CATEGORY_STOCK_QUERIES.get(categoria, "motorsport racing")
    url = (
        "https://api.pexels.com/v1/search?"
        + urllib.parse.urlencode({"query": query, "per_page": 15, "orientation": "landscape"})
    )
    req = urllib.request.Request(url, headers={"Authorization": PEXELS_API_KEY})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        print(f"    aviso: falha ao buscar foto no Pexels ({exc})", file=sys.stderr)
        return None
    photos = data.get("photos") or []
    if not photos:
        return None
    photo = random.choice(photos)
    src = photo.get("src", {})
    image_url = src.get("large") or src.get("medium") or src.get("original")
    if not image_url:
        return None
    return {"url": image_url, "credit_name": "", "credit_url": "", "provider": "Pexels"}


# Ordem de tentativa: Unsplash primeiro, Pexels como reserva.
STOCK_IMAGE_PROVIDERS = [fetch_unsplash_image, fetch_pexels_image]


def get_article_image(categoria: str) -> dict:
    """Tenta cada provedor na ordem; devolve {} se nenhum achar nada (aí o
    template usa o placeholder colorido por categoria)."""
    if DRY_RUN:
        return {}
    for provider_fn in STOCK_IMAGE_PROVIDERS:
        result = provider_fn(categoria)
        if result:
            return result
    return {}


# --------------------------------------------------------------------------
# Gravação do Markdown
# --------------------------------------------------------------------------

def toml_list(items) -> str:
    return "[" + ", ".join(f'"{str(i).replace(chr(34), "")}"' for i in items) + "]"


def write_post(
    article: dict,
    source_names: list[str],
    source_urls: list[str],
    date: datetime,
    image_info: dict | None = None,
) -> Path:
    slug = slugify(article["titulo"])[:70] or "materia"
    filename = f"{date:%Y-%m-%d}-{slug}.md"
    path = POSTS_DIR / filename
    image_info = image_info or {}

    def esc(s: str) -> str:
        return str(s).replace('"', "'")

    frontmatter = "\n".join(
        [
            "+++",
            f'title = "{esc(article["titulo"])}"',
            f"date = {date:%Y-%m-%dT%H:%M:%SZ}",
            f'summary = "{esc(article.get("linha_fina", ""))}"',
            f'categories = {toml_list([article.get("categoria", ALLOWED_CATEGORIES[0])])}',
            f'tags = {toml_list(article.get("tags", []))}',
            f"sources = {toml_list(source_names)}",
            f"source_urls = {toml_list(source_urls)}",
            f'image = "{esc(image_info.get("url", ""))}"',
            f'image_credit_name = "{esc(image_info.get("credit_name", ""))}"',
            f'image_credit_url = "{esc(image_info.get("credit_url", ""))}"',
            f'image_provider = "{esc(image_info.get("provider", ""))}"',
            "+++",
            "",
        ]
    )
    body = article.get("corpo_markdown", "").strip()
    path.write_text(frontmatter + body + "\n", encoding="utf-8")
    return path


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    POSTS_DIR.mkdir(parents=True, exist_ok=True)
    config = yaml.safe_load(SOURCES_FILE.read_text(encoding="utf-8"))
    feeds = config.get("feeds", [])
    seen = load_seen()

    candidates = collect_all_candidates(feeds, seen)
    print(f"\nTotal de manchetes novas coletadas: {len(candidates)}")

    if not candidates:
        save_seen(seen)
        print("Nada novo. Concluído.")
        return 0

    print("\nAgrupando e classificando manchetes...")
    try:
        groups = cluster_and_classify(candidates)
    except Exception as exc:  # noqa: BLE001
        print(f"erro ao agrupar/classificar: {exc}", file=sys.stderr)
        # NÃO marca como visto: falha no agrupamento é normalmente transitória
        # (rede, resposta inesperada). Assim as mesmas manchetes são
        # tentadas de novo na próxima rodada em vez de se perderem.
        return 1

    created = 0
    grouped_ids: set[int] = set()

    for group in groups:
        ids = [i for i in group.get("ids", []) if 0 <= i < len(candidates)]
        categoria = group.get("categoria", "DESCARTAR")
        grouped_ids.update(ids)
        if not ids:
            continue

        group_candidates = [candidates[i] for i in ids]
        links = [c["link"] for c in group_candidates]

        if categoria not in ALLOWED_CATEGORIES:
            print(f"  descartado (fora do escopo): {group_candidates[0]['title'][:70]}")
            for link in links:
                seen.add(link)
            continue

        titles_preview = " | ".join(c["title"][:50] for c in group_candidates)
        print(f"\n  + [{categoria}] {titles_preview}")

        try:
            source_blocks: list[tuple[str, str, str | None]] = []
            for c in group_candidates[:4]:
                if "_full_text" in c:
                    text = c["_full_text"]
                else:
                    downloaded = trafilatura.fetch_url(c["link"])
                    c["_source_html"] = downloaded
                    text = None
                    if downloaded:
                        text = trafilatura.extract(downloaded, include_comments=False)
                    if not text or len(text) < 120:
                        text = c.get("summary", "")
                if text and len(text) >= 80:
                    source_blocks.append((c["name"], text, c.get("extra_instructions")))

            if not source_blocks:
                print("    pulei: nenhum texto-fonte utilizável")
                for link in links:
                    seen.add(link)
                continue

            article = rewrite_with_claude(source_blocks)
            try:
                article = factcheck_with_claude(article, source_blocks)
            except Exception as exc:  # noqa: BLE001
                print(f"    aviso: revisão de fatos falhou, publicando rascunho ({exc})", file=sys.stderr)

            source_names = [c["name"] for c in group_candidates]
            publish_date = max(c["date"] for c in group_candidates)
            image_info = get_ai_variation_image(group_candidates)
            if not image_info:
                image_info = get_article_image(article.get("categoria", ALLOWED_CATEGORIES[0]))
            write_post(article, source_names, links, publish_date, image_info)
            created += 1
        except Exception as exc:  # noqa: BLE001
            print(f"    erro ao gerar/gravar: {exc}", file=sys.stderr)
        finally:
            for link in links:
                seen.add(link)

    for i, c in enumerate(candidates):
        if i not in grouped_ids:
            seen.add(c["link"])

    save_seen(seen)
    print(f"\nConcluído: {created} matéria(s) nova(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
