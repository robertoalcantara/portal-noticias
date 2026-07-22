#!/usr/bin/env python3
"""
Publica os cards de Stories (gerados por pipeline/cards.py) como Stories de
verdade no Instagram (@gridgeral), E também um post de carrossel no FEED
(mesmas imagens dos cards, com legenda) -- via Instagram Graph API (Meta).

Roda como workflow SEPARADO (.github/workflows/instagram-stories.yml),
agendado pra alguns minutos depois do funil principal ("Atualizar
notícias") -- a API do Instagram busca a imagem por URL pública, então ela
precisa estar no ar (deploy do Cloudflare Pages já feito) antes de tentar
publicar. Não depende de qual workflow gerou a matéria (funil automático,
modo manual etc.) -- só varre o que já está publicado em
site/content/posts/ + site/static/images/cards/ e publica o que ainda não
foi publicado.

Duas publicações são feitas por matéria, de forma independente (uma pode
falhar/ficar pra trás sem afetar a outra). Só matérias com menos de
MAX_CARD_AGE_HOURS (hoje 1 dia) geram Story/post novo -- conteúdo velho
nunca é publicado, mesmo que sobre cota depois (ver
_iter_recent_articles_with_cards()):
  - Stories: cada card vira um Story separado (1 publicação por imagem).
    IMPORTANTE: a API de publicação da Meta NÃO aceita legenda, link,
    sticker de link nem nenhum outro texto em Stories publicados via API
    -- é uma limitação da própria plataforma (só o app oficial permite
    adicionar esses elementos manualmente), então o Story sai só com a
    imagem do card, sem nenhum texto/link.
  - Feed: TODOS os cards da matéria viram um único post de carrossel (ou
    uma imagem única, se só houver 1 card), com legenda montada a partir
    do título/resumo/categoria da matéria MAIS o link direto da matéria
    como texto (Instagram não permite link clicável na legenda, mas pelo
    menos dá pra copiar) E um call-to-action pro "link na bio" -- as duas
    formas de link que a API permite. Pra o "link na bio" funcionar de
    verdade, a bio da conta @gridgeral precisa apontar pro site (isso é
    configurado manualmente no app do Instagram, não tem API pra isso).

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
                                 segurança pra Stories, por rodada.
  MAX_INSTAGRAM_FEED_POSTS_PER_DAY (opcional, padrão: 10) -- corte de
                                 segurança pra posts de feed, por rodada
                                 (independente da cota de Stories).
                                 Hoje o Instagram limita a API a 100
                                 publicações por conta a cada 24h, somando
                                 TODOS os tipos de conteúdo publicado via
                                 API (Stories + feed + reels; um carrossel
                                 conta como 1 publicação, não uma por
                                 imagem) -- posts feitos manualmente pelo
                                 app/site do Instagram não contam nesse
                                 limite. Ver
                                 developers.facebook.com/docs/instagram-platform/content-publishing
                                 pro número atual, a Meta já mudou esse
                                 limite antes. As duas cotas acima somadas
                                 (20 + 10 = 30) ficam BEM abaixo dos 100,
                                 de propósito, mesmo que as duas rodadas
                                 batam o teto no mesmo dia.

Se INSTAGRAM_ACCESS_TOKEN ou INSTAGRAM_BUSINESS_ACCOUNT_ID não estiverem
definidas, o script sai silenciosamente sem fazer nada -- funcionalidade
opcional, mesmo padrão de GEMINI_API_KEY/DEEPSEEK_API_KEY no resto do
pipeline (não quebra o resto do projeto pra quem não configurou isso).

Controle do que já foi postado:
  - pipeline/instagram_posted.json       -- Stories, um registro por card
    publicado (card_id = "<matéria>/<arquivo>.png").
  - pipeline/instagram_feed_posted.json  -- Feed, um registro por MATÉRIA
    publicada (article_id = "<matéria>", já que o post de feed é um só
    por matéria, carrossel ou imagem única).
Nunca reposta a mesma coisa duas vezes, e cada arquivo serve também pra
calcular quantas publicações daquele tipo já aconteceram nas últimas 24h
(pro corte de segurança de cada um, acima).
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
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
FEED_POSTED_FILE = ROOT / "pipeline" / "instagram_feed_posted.json"
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
MAX_FEED_POSTS_PER_DAY = int(os.environ.get("MAX_INSTAGRAM_FEED_POSTS_PER_DAY", "10"))

# Stories e feed são conteúdo do "agora" -- não faz sentido postar (ou
# gerar post de feed) de uma matéria de mais de 1 dia só porque a cota
# de um dia mais cheio não alcançou. Cards de matérias mais velhas que
# isso ficam pra trás de propósito (nunca são postados, mesmo que sobre
# cota depois). Vale tanto pra Stories quanto pro feed -- ambos usam a
# mesma leva de matérias "recentes com cards".
MAX_CARD_AGE_HOURS = 24

# carrossel do Instagram aceita entre 2 e 10 itens -- se uma matéria tiver
# mais cards que isso (não deveria, pipeline/cards.py gera poucos), corta
# nos 10 primeiros em vez de falhar.
MAX_CAROUSEL_ITEMS = 10

# pausa entre publicações seguidas nesta rodada -- cortesia com a API,
# evita rajada de requisições.
SLEEP_BETWEEN_POSTS_SECONDS = 5

_FRONTMATTER_PATTERN = re.compile(r"^\+\+\+\n(.*?)\n\+\+\+\n", re.S)

# hashtag fixa da marca, em todo post de feed.
BRAND_HASHTAGS = ["#GridGeral", "#Automobilismo"]


def _read_site_base_url() -> str:
    """baseURL configurado em site/hugo.toml -- pra montar a URL pública de
    cada imagem de card (a API do Instagram busca a imagem por URL, ela
    precisa estar no ar de verdade, não só no repo)."""
    text = HUGO_CONFIG_FILE.read_text(encoding="utf-8")
    m = re.search(r'^baseURL\s*=\s*"([^"]+)"', text, re.M)
    if not m:
        raise RuntimeError("não encontrei baseURL em site/hugo.toml")
    return m.group(1).rstrip("/")


def _load_json_list(path: Path) -> list[dict]:
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        print(f"aviso: {path} ilegível, tratando como vazio", file=sys.stderr)
        return []


def _save_json_list(path: Path, records: list[dict]) -> None:
    path.write_text(
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


def create_media_container(
    image_url: str,
    *,
    media_type: str | None = None,
    is_carousel_item: bool = False,
    caption: str | None = None,
    children: list[str] | None = None,
) -> str:
    """Cria um container de mídia genérico (a API baixa a imagem/monta o
    carrossel de forma assíncrona -- ver wait_container_ready). Um único
    endpoint (/media) serve pra Stories, item de carrossel, carrossel
    completo (com `children`) e post de imagem única no feed -- o que
    muda é a combinação de parâmetros:
      - Story:            media_type="STORIES", image_url=...
      - item de carrossel: is_carousel_item=True, image_url=...
      - carrossel (pai):   media_type="CAROUSEL", children=[...ids...], caption=...
      - imagem única (feed): image_url=..., caption=... (sem media_type)
    """
    params: dict = {"access_token": ACCESS_TOKEN}
    if children is not None:
        params["media_type"] = "CAROUSEL"
        params["children"] = ",".join(children)
    else:
        params["image_url"] = image_url
        if media_type:
            params["media_type"] = media_type
        if is_carousel_item:
            params["is_carousel_item"] = "true"
    if caption:
        params["caption"] = caption

    data = _api_request(f"{GRAPH_API_BASE}/{IG_USER_ID}/media", params)
    creation_id = data.get("id")
    if not creation_id:
        raise RuntimeError(f"resposta sem 'id' ao criar container: {data}")
    return creation_id


def wait_container_ready(creation_id: str, attempts: int = 10, delay_seconds: int = 3) -> None:
    """Espera o container terminar de processar (status_code FINISHED)
    antes de publicar -- pra imagem costuma ser rápido, mas a Meta
    recomenda checar em vez de publicar direto. Vale tanto pra Stories
    quanto pra itens/carrossel de feed."""
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


def publish_media(creation_id: str) -> str:
    """Publica de verdade um container já pronto (Story, imagem única ou
    carrossel completo -- mesmo endpoint pros três)."""
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
    creation_id = create_media_container(image_url, media_type="STORIES")
    wait_container_ready(creation_id)
    return publish_media(creation_id)


def post_feed_post(image_urls: list[str], caption: str) -> str:
    """Publica UM post de feed a partir de uma ou mais imagens: imagem
    única se só houver uma URL, carrossel (2 a MAX_CAROUSEL_ITEMS itens)
    se houver mais. Devolve o media id publicado; levanta exceção em
    qualquer falha (quem chama trata isso como não-fatal, ver main())."""
    urls = image_urls[:MAX_CAROUSEL_ITEMS]

    if len(urls) == 1:
        creation_id = create_media_container(urls[0], caption=caption)
        wait_container_ready(creation_id)
        return publish_media(creation_id)

    item_ids: list[str] = []
    for url in urls:
        item_id = create_media_container(url, is_carousel_item=True)
        wait_container_ready(item_id)
        item_ids.append(item_id)

    carousel_id = create_media_container("", children=item_ids, caption=caption)
    wait_container_ready(carousel_id)
    return publish_media(carousel_id)


def _article_url(base_url: str, filename_base: str, date: datetime) -> str:
    """Reconstrói o link público da matéria a partir do nome do arquivo
    (post_filename_base() em generate.py: "{AAAA-MM-DD}-{slug}") e do
    permalink configurado em site/hugo.toml ([permalinks] posts =
    "/:year/:month/:slug/"). Usado pra colar o link da matéria na
    legenda do post de feed (Instagram não deixa link clicável na
    legenda, mas o texto puro pelo menos dá pra copiar)."""
    slug = filename_base[11:]  # remove o prefixo "AAAA-MM-DD-"
    return f"{base_url}/{date:%Y}/{date:%m}/{slug}/"


def _post_date(frontmatter: dict) -> datetime | None:
    date = frontmatter.get("date")
    if isinstance(date, datetime):
        return date if date.tzinfo else date.replace(tzinfo=timezone.utc)
    return None


def _hashtag(text: str) -> str:
    """Normaliza uma string livre (categoria, tag) pra virar hashtag:
    tira acento, mantém só letras/números, sem espaço. Ex.: "Fórmula 1"
    -> "#Formula1". Devolve "" se não sobrar nada aproveitável."""
    normalized = unicodedata.normalize("NFKD", text)
    ascii_only = "".join(c for c in normalized if not unicodedata.combining(c))
    cleaned = re.sub(r"[^0-9A-Za-z]", "", ascii_only)
    return f"#{cleaned}" if cleaned else ""


def build_feed_caption(frontmatter: dict, article_url: str) -> str:
    """Monta a legenda do post de feed a partir do frontmatter do post
    (title/summary/categories/tags -- ver write_post() em generate.py) E
    do link da matéria. Instagram não permite link clicável na legenda,
    então colamos a URL como texto puro (pelo menos dá pra copiar) E
    também pedimos pra acessar o link na bio -- as duas formas possíveis
    pela API, já que não existe parâmetro de link/sticker pra legenda ou
    pra Stories (ver post_story(); Stories não aceitam legenda nem link
    nenhum via API, é uma limitação da própria Meta)."""
    title = str(frontmatter.get("title", "")).strip()
    summary = str(frontmatter.get("summary", "")).strip()
    categories = frontmatter.get("categories") or []
    tags = frontmatter.get("tags") or []

    hashtags = list(BRAND_HASHTAGS)
    if categories:
        tag = _hashtag(str(categories[0]))
        if tag:
            hashtags.append(tag)
    for t in tags[:3]:
        tag = _hashtag(str(t))
        if tag and tag not in hashtags:
            hashtags.append(tag)

    parts = [title]
    if summary:
        parts.append(summary)
    parts.append(f"Matéria completa: {article_url}\n(ou no link da bio \U0001F446)")
    parts.append(" ".join(hashtags))
    return "\n\n".join(p for p in parts if p)


def _iter_recent_articles_with_cards() -> list[tuple[datetime, str, dict, list[Path]]]:
    """Varre site/content/posts em busca de matérias com cards em
    site/static/images/cards/<mesmo-nome>/, mais novas que
    MAX_CARD_AGE_HOURS -- a mesma leva de matérias serve de base tanto
    pra Stories (find_pending_cards) quanto pro feed
    (find_pending_feed_articles). Devolve (data, filename_base,
    frontmatter, lista_de_cards_ordenada), da mais antiga pra mais nova."""
    if not POSTS_DIR.exists() or not CARDS_IMAGES_DIR.exists():
        return []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_CARD_AGE_HOURS)
    articles: list[tuple[datetime, str, dict, list[Path]]] = []
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
        card_paths = sorted(cards_dir.glob("*.png"))
        if not card_paths:
            continue
        articles.append((date, filename_base, frontmatter, card_paths))
    articles.sort(key=lambda item: item[0])
    return articles


def find_pending_cards(
    articles: list[tuple[datetime, str, dict, list[Path]]], already_posted: set[str]
) -> list[tuple[datetime, str, Path]]:
    """A partir da leva de matérias recentes com cards, devolve os cards
    individuais ainda não postados como Story: (data, identificador do
    card, caminho do arquivo), da mais antiga pra mais nova."""
    pending: list[tuple[datetime, str, Path]] = []
    for date, filename_base, _frontmatter, card_paths in articles:
        for card_path in card_paths:
            identifier = f"{filename_base}/{card_path.name}"
            if identifier in already_posted:
                continue
            pending.append((date, identifier, card_path))
    return pending


def find_pending_feed_articles(
    articles: list[tuple[datetime, str, dict, list[Path]]], already_posted: set[str]
) -> list[tuple[datetime, str, dict, list[Path]]]:
    """A partir da mesma leva de matérias recentes com cards, devolve as
    que ainda não geraram post de feed (um post por matéria inteira,
    carrossel ou imagem única -- ver post_feed_post)."""
    return [a for a in articles if a[1] not in already_posted]


def main() -> int:
    if not ACCESS_TOKEN or not IG_USER_ID:
        print(
            "INSTAGRAM_ACCESS_TOKEN/INSTAGRAM_BUSINESS_ACCOUNT_ID não "
            "configuradas -- pulando publicação no Instagram."
        )
        return 0

    base_url = os.environ.get("SITE_BASE_URL", "").strip() or _read_site_base_url()
    articles = _iter_recent_articles_with_cards()

    # --- Stories -----------------------------------------------------
    story_records = _load_json_list(POSTED_FILE)
    already_posted_stories = {r["card_id"] for r in story_records}
    stories_last_24h = _posts_last_24h(story_records)
    stories_quota = max(0, MAX_POSTS_PER_DAY - stories_last_24h)

    print(f"Stories nas últimas 24h: {stories_last_24h}/{MAX_POSTS_PER_DAY}")
    pending_cards = find_pending_cards(articles, already_posted_stories)
    print(f"Cards pendentes (< {MAX_CARD_AGE_HOURS}h, ainda não postados como Story): {len(pending_cards)}")

    to_post_stories = pending_cards[:stories_quota] if stories_quota > 0 else []
    if stories_quota <= 0:
        print("Cota diária de Stories (de segurança) atingida -- nada a fazer nesta rodada.")

    posted_stories_now = 0
    for i, (_date, identifier, card_path) in enumerate(to_post_stories):
        rel = card_path.relative_to(ROOT / "site" / "static")
        image_url = f"{base_url}/{rel.as_posix()}"
        print(f"  [Story {i + 1}/{len(to_post_stories)}] {identifier} -> {image_url}")
        try:
            media_id = post_story(image_url)
        except Exception as exc:  # noqa: BLE001
            print(f"    erro ao publicar Story: {exc}", file=sys.stderr)
            continue
        story_records.append(
            {
                "card_id": identifier,
                "image_url": image_url,
                "media_id": media_id,
                "posted_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        posted_stories_now += 1
        _save_json_list(POSTED_FILE, story_records)  # grava incrementalmente -- uma falha no meio não perde o que já foi postado
        time.sleep(SLEEP_BETWEEN_POSTS_SECONDS)

    print(f"✓ {posted_stories_now}/{len(to_post_stories)} Stories publicadas com sucesso.\n")

    # --- Feed (carrossel/imagem única) --------------------------------
    feed_records = _load_json_list(FEED_POSTED_FILE)
    already_posted_feed = {r["article_id"] for r in feed_records}
    feed_last_24h = _posts_last_24h(feed_records)
    feed_quota = max(0, MAX_FEED_POSTS_PER_DAY - feed_last_24h)

    print(f"Posts de feed nas últimas 24h: {feed_last_24h}/{MAX_FEED_POSTS_PER_DAY}")
    pending_feed = find_pending_feed_articles(articles, already_posted_feed)
    print(f"Matérias pendentes (< {MAX_CARD_AGE_HOURS}h, ainda sem post de feed): {len(pending_feed)}")

    to_post_feed = pending_feed[:feed_quota] if feed_quota > 0 else []
    if feed_quota <= 0:
        print("Cota diária de posts de feed (de segurança) atingida -- nada a fazer nesta rodada.")

    posted_feed_now = 0
    for i, (_date, filename_base, frontmatter, card_paths) in enumerate(to_post_feed):
        image_urls = []
        for card_path in card_paths[:MAX_CAROUSEL_ITEMS]:
            rel = card_path.relative_to(ROOT / "site" / "static")
            image_urls.append(f"{base_url}/{rel.as_posix()}")
        article_url = _article_url(base_url, filename_base, _date)
        caption = build_feed_caption(frontmatter, article_url)
        kind = "imagem única" if len(image_urls) == 1 else f"carrossel de {len(image_urls)}"
        print(f"  [Feed {i + 1}/{len(to_post_feed)}] {filename_base} ({kind})")
        try:
            media_id = post_feed_post(image_urls, caption)
        except Exception as exc:  # noqa: BLE001
            print(f"    erro ao publicar no feed: {exc}", file=sys.stderr)
            continue
        feed_records.append(
            {
                "article_id": filename_base,
                "image_urls": image_urls,
                "media_id": media_id,
                "posted_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        posted_feed_now += 1
        _save_json_list(FEED_POSTED_FILE, feed_records)  # grava incrementalmente
        time.sleep(SLEEP_BETWEEN_POSTS_SECONDS)

    print(f"✓ {posted_feed_now}/{len(to_post_feed)} posts de feed publicados com sucesso.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
