#!/usr/bin/env python3
"""
Publica os cards de Stories (gerados por pipeline/cards.py) como Stories de
verdade no Instagram (@gridgeral), via Instagram Graph API (Meta).

Roda como workflow SEPARADO (.github/workflows/instagram-stories.yml),
agendado pra alguns minutos depois do funil principal ("Atualizar
notícias") -- a API do Instagram busca a imagem por URL pública, então ela
precisa estar no ar (deploy do Cloudflare Pages já feito) antes de tentar
publicar. Não depende de qual workflow gerou a matéria (funil automático,
modo manual etc.) -- só varre o que já está publicado em
site/content/posts/ + site/static/images/cards/ e publica o que ainda não
foi publicado.

Pré-requisitos no Meta (ver README, seção "Publicação automática no
Instagram"), feitos manualmente uma vez só -- fluxo "Instagram API with
Instagram Login" (NÃO exige vincular Página do Facebook):
  1. Conta do Instagram (@gridgeral) convertida pra Business ou Creator.
  2. Um app em https://developers.facebook.com/ com o produto
     "Instagram" (Business Login for Instagram) adicionado.
  3. Um token de acesso de LONGA DURAÇÃO (~60 dias, renovável, começa
     com "IGA...") com as permissões instagram_business_basic +
     instagram_business_content_publish, trocado a partir de um token
     curto via GET graph.instagram.com/access_token?grant_type=
     ig_exchange_token&client_secret=...&access_token=....
  4. O ID numérico da conta do Instagram (NÃO é o @usuario) --
     GET https://graph.instagram.com/v21.0/me?fields=id,username&access_token=....

IMPORTANTE: um token "IGA..." desse fluxo só funciona contra o host
graph.instagram.com (usado abaixo em GRAPH_API_BASE) -- chamar
graph.facebook.com com ele dá erro 190 "Cannot parse access token".

Env vars:
  INSTAGRAM_ACCESS_TOKEN        (obrigatória) -- token de acesso de longa
                                 duração descrito acima.
  INSTAGRAM_BUSINESS_ACCOUNT_ID (obrigatória) -- ID numérico da conta
                                 profissional do Instagram.
  SITE_BASE_URL                 (opcional -- padrão: lê o baseURL de
                                 site/hugo.toml) -- de onde monta a URL
                                 pública de cada imagem de card.
  MAX_INSTAGRAM_POSTS_PER_DAY   (opcional, padrão: 20) -- corte de
                                 segurança por rodada: hoje o Instagram
                                 limita a API a 100 publicações por conta
                                 a cada 24h (conteúdo publicado via API --
                                 posts feitos manualmente pelo app/site do
                                 Instagram não contam nesse limite; ver
                                 developers.facebook.com/docs/instagram-platform/content-publishing
                                 pro número atual, a Meta já mudou esse
                                 limite antes). Ficamos BEM abaixo de
                                 propósito, com folga.

Se INSTAGRAM_ACCESS_TOKEN ou INSTAGRAM_BUSINESS_ACCOUNT_ID não estiverem
definidas, o script sai silenciosamente sem fazer nada -- funcionalidade
opcional, mesmo padrão de GEMINI_API_KEY/DEEPSEEK_API_KEY no resto do
pipeline (não quebra o resto do projeto pra quem não configurou isso).

Controle do que já foi postado: pipeline/instagram_posted.json (lista de
registros, um por card publicado -- ver _load_posted()/_save_posted()).
Nunca reposta o mesmo card, e serve também pra calcular quantas
publicações já aconteceram nas últimas 24h (pro corte de segurança acima).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import tomllib  # stdlib a partir do Python 3.11 (o workflow usa 3.12)
except ModuleNotFoundError:  # pragma: no cover - só acontece em Python <3.11
    import tomli as tomllib  # type: ignore[no-redef]

ROOT = Path(__file__).resolve().parent.parent
POSTS_DIR = ROOT / "site" / "content" / "posts"
CARDS_IMAGES_DIR = ROOT / "site" / "static" / "images" / "cards"
POSTED_FILE = ROOT / "pipeline" / "instagram_posted.json"
HUGO_CONFIG_FILE = ROOT / "site" / "hugo.toml"

GRAPH_API_VERSION = "v21.0"  # se a Meta descontinuar essa versão, só trocar aqui
# graph.instagram.com (não graph.facebook.com!) -- esse host é específico do
# fluxo "Instagram API with Instagram Login" (Business Login for Instagram),
# o que gera token começando com "IGA...". Existe também o fluxo antigo
# "Instagram API with Facebook Login" (conta vinculada a uma Página, token
# tipo "EAA...", host graph.facebook.com) -- os dois NÃO se misturam: um
# token do tipo errado pro host errado dá "Cannot parse access token" (erro
# 190), porque o parser de cada host só reconhece o formato do seu próprio
# fluxo. Ver README, seção "Publicação automática no Instagram".
GRAPH_API_BASE = f"https://graph.instagram.com/{GRAPH_API_VERSION}"

ACCESS_TOKEN = os.environ.get("INSTAGRAM_ACCESS_TOKEN", "").strip()
IG_USER_ID = os.environ.get("INSTAGRAM_BUSINESS_ACCOUNT_ID", "").strip()
MAX_POSTS_PER_DAY = int(os.environ.get("MAX_INSTAGRAM_POSTS_PER_DAY", "20"))

# Stories são conteúdo do "agora" -- não faz sentido postar Stories de uma
# matéria de vários dias atrás só porque a cota de um dia mais cheio não
# alcançou. Cards de matérias mais velhas que isso ficam pra trás de
# propósito (nunca são postados, mesmo que sobre cota depois).
MAX_CARD_AGE_HOURS = 48

# pausa entre publicações seguidas nesta rodada -- cortesia com a API,
# evita rajada de requisições.
SLEEP_BETWEEN_POSTS_SECONDS = 5

_FRONTMATTER_PATTERN = re.compile(r"^\+\+\+\n(.*?)\n\+\+\+\n", re.S)


def _read_site_base_url() -> str:
    """baseURL configurado em site/hugo.toml -- pra montar a URL pública de
    cada imagem de card (a API do Instagram busca a imagem por URL, ela
    precisa estar no ar de verdade, não só no repo)."""
    text = HUGO_CONFIG_FILE.read_text(encoding="utf-8")
    m = re.search(r'^baseURL\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise RuntimeError("não encontrei baseURL em site/hugo.toml")
    return m.group(1).rstrip("/")


def _load_posted() -> list[dict]:
    if not POSTED_FILE.exists():
        return []
    try:
        return json.loads(POSTED_FILE.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        print(f"aviso: {POSTED_FILE} ilegível, tratando como vazio", file=sys.stderr)
        return []


def _save_posted(records: list[dict]) -> None:
    POSTED_FILE.write_text(
        json.dumps(records, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _posts_last_24h(records: list[dict]) -> int:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    count = 0
    for r in records:
        try:
            ts = datetime.fromisoformat(r["posted_at"])
        except Exception:  # noqa: BLE001
            continue
        if ts >= cutoff:
            count += 1
    return count


def _api_request(url: str, params: dict, method: str = "POST") -> dict:
    query = urllib.parse.urlencode(params).encode("utf-8")
    if method == "GET":
        req = urllib.request.Request(f"{url}?{query.decode()}", method="GET")
    else:
        req = urllib.request.Request(url, data=query, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:500]
        raise RuntimeError(f"HTTP {exc.code} de {url}: {detail}") from exc


def create_story_container(image_url: str) -> str:
    """Passo 1: cria um container de mídia pro Story (a API baixa a
    imagem da URL informada -- assíncrono, ver wait_container_ready)."""
    data = _api_request(
        f"{GRAPH_API_BASE}/{IG_USER_ID}/media",
        {
            "image_url": image_url,
            "media_type": "STORIES",
            "access_token": ACCESS_TOKEN,
        },
    )
    creation_id = data.get("id")
    if not creation_id:
        raise RuntimeError(f"resposta sem 'id' ao criar container: {data}")
    return creation_id


def wait_container_ready(creation_id: str, attempts: int = 10, delay_seconds: int = 3) -> None:
    """Passo 2: espera o container terminar de processar (status_code
    FINISHED) antes de publicar -- pra imagem costuma ser rápido, mas a
    Meta recomenda checar em vez de publicar direto."""
    status = None
    for _ in range(attempts):
        data = _api_request(
            f"{GRAPH_API_BASE}/{creation_id}",
            {"fields": "status_code", "access_token": ACCESS_TOKEN},
            method="GET",
        )
        status = data.get("status_code")
        if status == "FINISHED":
            return
        if status == "ERROR":
            raise RuntimeError(f"container {creation_id} falhou no processamento: {data}")
        time.sleep(delay_seconds)
    raise RuntimeError(f"container {creation_id} não ficou pronto a tempo (status={status!r})")


def publish_story(creation_id: str) -> str:
    """Passo 3: publica de verdade o container já pronto."""
    data = _api_request(
        f"{GRAPH_API_BASE}/{IG_USER_ID}/media_publish",
        {"creation_id": creation_id, "access_token": ACCESS_TOKEN},
    )
    media_id = data.get("id")
    if not media_id:
        raise RuntimeError(f"resposta sem 'id' ao publicar: {data}")
    return media_id


def post_story(image_url: str) -> str:
    """Publica UM Story a partir da URL pública de uma imagem -- orquestra
    os 3 passos (criar container / esperar / publicar). Devolve o media id
    publicado; levanta exceção em qualquer falha (quem chama trata isso
    como não-fatal, ver main())."""
    creation_id = create_story_container(image_url)
    wait_container_ready(creation_id)
    return publish_story(creation_id)


def _post_date(frontmatter: dict) -> datetime | None:
    date = frontmatter.get("date")
    if isinstance(date, datetime):
        return date if date.tzinfo else date.replace(tzinfo=timezone.utc)
    return None


def find_pending_cards(already_posted: set[str]) -> list[tuple[datetime, str, Path]]:
    """Varre site/content/posts em busca de matérias com cards em
    site/static/images/cards/<mesmo-nome>/ ainda não postados no
    Instagram, mais novas que MAX_CARD_AGE_HOURS. Devolve uma lista de
    (data_da_matéria, identificador_do_card, caminho_do_arquivo), da mais
    antiga pra mais nova (FIFO -- nada fica preso atrás de conteúdo mais
    novo pra sempre)."""
    if not POSTS_DIR.exists() or not CARDS_IMAGES_DIR.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_CARD_AGE_HOURS)
    pending: list[tuple[datetime, str, Path]] = []
    for post_path in POSTS_DIR.glob("*.md"):
        filename_base = post_path.stem
        cards_dir = CARDS_IMAGES_DIR / filename_base
        if not cards_dir.exists():
            continue
        try:
            text = post_path.read_text(encoding="utf-8")
            match = _FRONTMATTER_PATTERN.match(text)
            frontmatter = tomllib.loads(match.group(1)) if match else {}
        except Exception:  # noqa: BLE001
            frontmatter = {}
        date = _post_date(frontmatter)
        if date is None or date < cutoff:
            continue
        for card_path in sorted(cards_dir.glob("*.png")):
            identifier = f"{filename_base}/{card_path.name}"
            if identifier in already_posted:
                continue
            pending.append((date, identifier, card_path))
    pending.sort(key=lambda item: item[0])
    return pending


def main() -> int:
    if not ACCESS_TOKEN or not IG_USER_ID:
        print(
            "INSTAGRAM_ACCESS_TOKEN/INSTAGRAM_BUSINESS_ACCOUNT_ID não "
            "configuradas -- pulando publicação no Instagram."
        )
        return 0

    base_url = os.environ.get("SITE_BASE_URL", "").strip() or _read_site_base_url()

    records = _load_posted()
    already_posted = {r["card_id"] for r in records}
    posted_last_24h = _posts_last_24h(records)
    remaining_quota = max(0, MAX_POSTS_PER_DAY - posted_last_24h)

    print(f"Publicações nas últimas 24h: {posted_last_24h}/{MAX_POSTS_PER_DAY}")
    if remaining_quota <= 0:
        print("Cota diária (de segurança) atingida -- nada a fazer nesta rodada.")
        return 0

    pending = find_pending_cards(already_posted)
    print(f"Cards pendentes (< {MAX_CARD_AGE_HOURS}h, ainda não postados): {len(pending)}")
    if not pending:
        return 0

    to_post = pending[:remaining_quota]
    print(f"Publicando até {len(to_post)} Stories nesta rodada (cota restante: {remaining_quota})...")

    posted_now = 0
    for i, (_date, identifier, card_path) in enumerate(to_post):
        rel = card_path.relative_to(ROOT / "site" / "static")
        image_url = f"{base_url}/{rel.as_posix()}"
        print(f"  [{i + 1}/{len(to_post)}] {identifier} -> {image_url}")
        try:
            media_id = post_story(image_url)
        except Exception as exc:  # noqa: BLE001
            print(f"    erro ao publicar: {exc}", file=sys.stderr)
            continue
        records.append(
            {
                "card_id": identifier,
                "image_url": image_url,
                "media_id": media_id,
                "posted_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        posted_now += 1
        _save_posted(records)  # grava incrementalmente -- uma falha no meio não perde o que já foi postado
        if i < len(to_post) - 1:
            time.sleep(SLEEP_BETWEEN_POSTS_SECONDS)

    print(f"\n✓ {posted_now}/{len(to_post)} Stories publicadas com sucesso.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
