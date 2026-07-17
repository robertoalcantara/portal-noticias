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
  CLUSTER_MODEL      (opcional, padrão: claude-sonnet-5 — agrupar/classificar)
  MAX_PER_FEED       (opcional, padrão: 4 — manchetes novas por fonte/rodada)
  MAX_SOURCE_CHARS   (opcional, padrão: 6000 — corte de CADA texto-fonte)
  DRY_RUN            (opcional, "1" para não chamar a API — usa texto de teste)
"""

from __future__ import annotations

import json
import os
import re
import sys
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
CLUSTER_MODEL = os.environ.get("CLUSTER_MODEL", "claude-sonnet-5")
MAX_PER_FEED = int(os.environ.get("MAX_PER_FEED", "4"))
MAX_SOURCE_CHARS = int(os.environ.get("MAX_SOURCE_CHARS", "6000"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"

# Categorias que o portal cobre. Qualquer manchete fora disso é descartada
# na etapa de classificação (não é gerada matéria).
ALLOWED_CATEGORIES = ["Kart", "F1", "F2", "F3", "F4", "GT3", "WEC", "Indy", "NASCAR"]

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
            data = trafilatura.bare_extraction(article_html, with_metadata=True)
        except Exception as exc:  # noqa: BLE001
            print(f"    aviso: falha ao ler {link} ({exc})", file=sys.stderr)
            continue
        if not data or not data.get("text") or len(data["text"]) < 120:
            continue
        title = data.get("title") or link
        date = datetime.now(timezone.utc)
        raw_date = data.get("date")
        if raw_date:
            try:
                date = datetime.fromisoformat(raw_date).replace(tzinfo=timezone.utc)
            except Exception:  # noqa: BLE001
                pass
        out.append(
            {
                "name": name,
                "title": title,
                "link": link,
                "summary": data["text"][:500],
                "date": date,
                "_full_text": data["text"],
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
        lines.append(f"id {i} | fonte: {c['name']} | título: {c['title']} | resumo: {summary}")
    user_content = "\n".join(lines)

    client = Anthropic()
    message = client.messages.create(
        model=CLUSTER_MODEL,
        max_tokens=2000,
        system=CLUSTER_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    raw = "".join(block.text for block in message.content if block.type == "text")
    data = parse_model_json(raw)
    return data.get("grupos", [])


def rewrite_with_claude(source_blocks: list[tuple[str, str]]) -> dict:
    """source_blocks: lista de (nome_da_fonte, texto). Gera UMA matéria."""
    if DRY_RUN:
        names = ", ".join(n for n, _ in source_blocks)
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
    for name, text in source_blocks:
        parts.append(f"[Fonte: {name}]\n{text[:MAX_SOURCE_CHARS]}")
    user_content = "\n\n---\n\n".join(parts)

    message = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    raw = "".join(block.text for block in message.content if block.type == "text")
    return parse_model_json(raw)


def factcheck_with_claude(article: dict, source_blocks: list[tuple[str, str]]) -> dict:
    """Segunda passada: revisa o rascunho contra os textos-fonte originais."""
    if DRY_RUN:
        return article

    from anthropic import Anthropic

    client = Anthropic()
    parts = [f"[Fonte: {name}]\n{text[:MAX_SOURCE_CHARS]}" for name, text in source_blocks]
    sources_text = "\n\n---\n\n".join(parts)
    user_content = (
        f"TEXTOS-FONTE:\n{sources_text}\n\n"
        f"RASCUNHO (JSON):\n{json.dumps(article, ensure_ascii=False)}"
    )
    message = client.messages.create(
        model=FACTCHECK_MODEL,
        max_tokens=1500,
        system=FACTCHECK_SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    raw = "".join(block.text for block in message.content if block.type == "text")
    return parse_model_json(raw)


# --------------------------------------------------------------------------
# Gravação do Markdown
# --------------------------------------------------------------------------

def toml_list(items) -> str:
    return "[" + ", ".join(f'"{str(i).replace(chr(34), "")}"' for i in items) + "]"


def write_post(article: dict, source_names: list[str], source_urls: list[str], date: datetime) -> Path:
    slug = slugify(article["titulo"])[:70] or "materia"
    filename = f"{date:%Y-%m-%d}-{slug}.md"
    path = POSTS_DIR / filename

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
            f'image = "{esc(article.get("image", ""))}"',
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
        for c in candidates:
            seen.add(c["link"])
        save_seen(seen)
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
            source_blocks: list[tuple[str, str]] = []
            for c in group_candidates[:4]:
                if "_full_text" in c:
                    text = c["_full_text"]
                else:
                    downloaded = trafilatura.fetch_url(c["link"])
                    text = None
                    if downloaded:
                        text = trafilatura.extract(downloaded, include_comments=False)
                    if not text or len(text) < 120:
                        text = c.get("summary", "")
                if text and len(text) >= 80:
                    source_blocks.append((c["name"], text))

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
            write_post(article, source_names, links, publish_date)
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
