#!/usr/bin/env python3
"""
Gera matérias para o portal a partir de feeds RSS.

Fluxo:
  1. Lê a lista de feeds em sources.yaml
  2. Para cada matéria nova (não vista antes), baixa o texto da fonte
  3. Pede ao Claude (modelo rápido/barato) para produzir um texto ORIGINAL em
     português (fatos + resumo), sempre com crédito e link para a fonte
  4. Passa o texto gerado por uma SEGUNDA revisão com um modelo mais forte
     (Sonnet), que compara cada afirmação contra o texto-fonte original e
     corrige qualquer nome, número, nacionalidade ou resultado inventado
  5. Grava um arquivo Markdown em site/content/posts/ (Hugo publica sozinho)

O script é resiliente: erros em uma matéria/feed não derrubam a execução inteira.
Se a revisão falhar, a matéria da primeira passada é publicada mesmo assim
(evita perder a matéria por causa de um erro na segunda chamada).
Rode localmente com `python pipeline/generate.py` ou deixe o GitHub Actions rodar.

Variáveis de ambiente:
  ANTHROPIC_API_KEY  (obrigatória, exceto em DRY_RUN)
  MODEL              (opcional, padrão: claude-haiku-4-5-20251001 — geração)
  FACTCHECK_MODEL    (opcional, padrão: claude-sonnet-5 — revisão/checagem)
  MAX_PER_FEED       (opcional, padrão: 5 — quantas matérias novas por feed/rodada)
  MAX_SOURCE_CHARS   (opcional, padrão: 6000 — corte do texto-fonte, controla custo)
  DRY_RUN            (opcional, "1" para não chamar a API — usa um texto de teste)
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

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
MAX_PER_FEED = int(os.environ.get("MAX_PER_FEED", "5"))
MAX_SOURCE_CHARS = int(os.environ.get("MAX_SOURCE_CHARS", "6000"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"

SYSTEM_PROMPT = """\
Você é um editor de um portal brasileiro de notícias de automobilismo.
A partir do material-fonte fornecido, produza uma matéria ORIGINAL em português do Brasil.

Regras obrigatórias:
- Escreva com suas próprias palavras. NÃO copie nem parafraseie frase a frase o texto-fonte.
- Baseie-se apenas nos FATOS presentes no material. Não invente dados, números, aspas ou nomes.
- Se o material for insuficiente, escreva uma nota curta em vez de preencher com suposições.
- Tom jornalístico, direto, sem sensacionalismo.
- A fonte original será sempre creditada e linkada pelo sistema; você não precisa incluir o link.

Responda APENAS com um objeto JSON válido (sem markdown, sem crases), no formato:
{
  "titulo": "título curto e informativo",
  "linha_fina": "uma frase de resumo (o 'dek')",
  "categoria": "uma de: Fórmula 1, IndyCar, NASCAR, MotoGP, Endurance, Rally, Fórmula E, Automobilismo",
  "tags": ["até 4 tags curtas"],
  "corpo_markdown": "3 a 5 parágrafos em Markdown"
}"""

FACTCHECK_SYSTEM_PROMPT = """\
Você é o revisor de fatos (fact-checker) de um portal brasileiro de automobilismo.
Vai receber o TEXTO-FONTE original e um RASCUNHO em português gerado por outro
editor a partir dele. Seu trabalho é comparar o rascunho contra o texto-fonte
FRASE A FRASE e corrigir qualquer imprecisão.

Verifique com atenção especial:
- Nomes de pilotos, equipes, patrocinadores e circuitos
- Nacionalidades e afiliações (equipe, categoria, fabricante)
- Números: tempos, posições, datas, contagem de pontos, resultados
- Relações de causa e efeito (ex.: quem lidera o quê, quem superou quem)
- Qualquer detalhe que soe específico mas não apareça no texto-fonte

Regras:
- Se um dado do rascunho não está no texto-fonte e não pode ser inferido com
  segurança, REMOVA ou GENERALIZE a frase — nunca mantenha um dado não verificado.
- Corrija o dado errado quando o texto-fonte permitir confirmar o correto
  (ex.: nacionalidade errada → corrija ou remova a menção).
- Não adicione fatos novos que não estavam no rascunho nem no texto-fonte.
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


def entry_date(entry) -> datetime:
    for key in ("published_parsed", "updated_parsed"):
        val = entry.get(key)
        if val:
            return datetime(*val[:6], tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def fetch_source_text(url: str, fallback: str) -> str:
    """Baixa o texto do artigo; se falhar, usa o resumo do próprio feed."""
    try:
        downloaded = trafilatura.fetch_url(url)
        if downloaded:
            text = trafilatura.extract(downloaded, include_comments=False)
            if text and len(text) > 200:
                return text
    except Exception as exc:  # noqa: BLE001
        print(f"    aviso: não consegui baixar o texto ({exc})", file=sys.stderr)
    return re.sub(r"<[^>]+>", " ", fallback or "").strip()


def parse_model_json(raw: str) -> dict:
    """Extrai o JSON da resposta do modelo, tolerando crases ou texto extra."""
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end != -1:
        cleaned = cleaned[start : end + 1]
    # strict=False tolera quebras de linha literais dentro das strings JSON,
    # que os modelos às vezes produzem.
    return json.loads(cleaned, strict=False)


def rewrite_with_claude(title: str, source_text: str) -> dict:
    if DRY_RUN:
        return {
            "titulo": f"[TESTE] {title}",
            "linha_fina": "Matéria de teste gerada em modo DRY_RUN.",
            "categoria": "Automobilismo",
            "tags": ["teste"],
            "corpo_markdown": (
                "Este é um texto de teste gerado sem chamar a API.\n\n"
                "Defina `DRY_RUN=0` e a variável `ANTHROPIC_API_KEY` para gerar de verdade."
            ),
        }

    from anthropic import Anthropic

    client = Anthropic()
    user_content = (
        f"Título original da fonte: {title}\n\n"
        f"Material-fonte:\n{source_text[:MAX_SOURCE_CHARS]}"
    )
    message = client.messages.create(
        model=MODEL,
        max_tokens=1500,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": user_content}],
    )
    raw = "".join(block.text for block in message.content if block.type == "text")
    return parse_model_json(raw)


def factcheck_with_claude(article: dict, source_text: str) -> dict:
    """Segunda passada: usa um modelo mais forte para checar o rascunho
    contra o texto-fonte e corrigir detalhes inventados ou incorretos."""
    if DRY_RUN:
        return article

    from anthropic import Anthropic

    client = Anthropic()
    user_content = (
        f"TEXTO-FONTE:\n{source_text[:MAX_SOURCE_CHARS]}\n\n"
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


def toml_list(items) -> str:
    return "[" + ", ".join(f'"{str(i).replace(chr(34), "")}"' for i in items) + "]"


def write_post(article: dict, source_name: str, source_url: str, date: datetime) -> Path:
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
            f'categories = {toml_list([article.get("categoria", "Automobilismo")])}',
            f'tags = {toml_list(article.get("tags", []))}',
            f'source_name = "{esc(source_name)}"',
            f'source_url = "{esc(source_url)}"',
            "+++",
            "",
        ]
    )
    body = article.get("corpo_markdown", "").strip()
    path.write_text(frontmatter + body + "\n", encoding="utf-8")
    return path


def main() -> int:
    POSTS_DIR.mkdir(parents=True, exist_ok=True)
    config = yaml.safe_load(SOURCES_FILE.read_text(encoding="utf-8"))
    feeds = config.get("feeds", [])
    seen = load_seen()

    created = 0
    for feed in feeds:
        name, url = feed["name"], feed["url"]
        print(f"\n== {name} ==")
        try:
            parsed = feedparser.parse(url)
        except Exception as exc:  # noqa: BLE001
            print(f"  erro ao ler o feed: {exc}", file=sys.stderr)
            continue
        if parsed.bozo and not parsed.entries:
            print(f"  feed vazio ou inválido ({url})", file=sys.stderr)
            continue

        new_in_feed = 0
        for entry in parsed.entries:
            if new_in_feed >= MAX_PER_FEED:
                break
            link = entry.get("link")
            if not link or link in seen:
                continue

            title = entry.get("title", "Sem título")
            print(f"  + {title[:70]}")
            fallback = entry.get("summary", "") or entry.get("description", "")
            source_text = fetch_source_text(link, fallback)
            if len(source_text) < 120:
                print("    pulei: texto-fonte curto demais")
                seen.add(link)
                continue

            try:
                article = rewrite_with_claude(title, source_text)
                try:
                    article = factcheck_with_claude(article, source_text)
                except Exception as exc:  # noqa: BLE001
                    print(f"    aviso: revisão de fatos falhou, publicando rascunho ({exc})", file=sys.stderr)
                write_post(article, name, link, entry_date(entry))
                created += 1
                new_in_feed += 1
            except Exception as exc:  # noqa: BLE001
                print(f"    erro ao gerar/gravar: {exc}", file=sys.stderr)
            finally:
                seen.add(link)

    save_seen(seen)
    print(f"\nConcluído: {created} matéria(s) nova(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
