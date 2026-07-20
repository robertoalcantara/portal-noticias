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
  ANTHROPIC_API_KEY  (obrigatória, exceto em DRY_RUN ou se DEEPSEEK_API_KEY
                     estiver definida — nesse caso o Claude não é chamado)
  MODEL              (opcional, padrão: claude-haiku-4-5-20251001 — geração)
  FACTCHECK_MODEL    (opcional, padrão: claude-haiku-4-5-20251001 — revisão/checagem)
  CLUSTER_MODEL      (opcional, padrão: claude-haiku-4-5-20251001 — agrupar/classificar)
  MAX_PER_FEED       (opcional, padrão: 4 — manchetes novas por fonte/rodada)
  MAX_SOURCE_CHARS   (opcional, padrão: 6000 — corte de CADA texto-fonte)
  RECENT_CONTEXT_WINDOW_HOURS (opcional, padrão: 72 — janela de matérias já
                     publicadas usada como contexto no agrupamento, pra não
                     publicar nada redundante/já superado por notícia mais
                     recente do mesmo evento)
  RECENT_CONTEXT_MAX_ITEMS    (opcional, padrão: 60 — teto de matérias
                     recentes incluídas nesse contexto, pra não estourar o
                     tamanho do prompt em rodadas com muito histórico)
  MANUAL_URLS        (opcional — modo manual: um ou mais links de
                     matéria-fonte, separados por vírgula ou quebra de
                     linha. Quando definida, o script IGNORA sources.yaml
                     e o funil automático inteiro (coleta/agrupamento) e
                     processa só esses links como fontes de UMA única
                     matéria — útil pra publicar manualmente uma notícia
                     específica a partir do link, sem esperar o RSS/
                     listagem pegar. Ver run_manual_mode().)
  DRY_RUN            (opcional, "1" para não chamar a API — usa texto de teste)
  GEMINI_API_KEY      (obrigatória para gerar imagem — sem ela, ou se a
                       matéria-fonte não tiver imagem, ou se a chamada
                       falhar, a matéria fica sem imagem)
  GEMINI_IMAGE_MODEL  (opcional, padrão: gemini-2.5-flash-image — modelo de
                       geração/edição de imagem "Nano Banana" do Gemini)
  DEEPSEEK_API_KEY    (opcional — se definida, o DeepSeek passa a ser o
                       ÚNICO modelo usado nas 3 chamadas de texto (agrupar,
                       escrever, checar fatos): NÃO há fallback pro Claude
                       se o DeepSeek falhar (rede, quota, resposta
                       vazia/inválida) — o erro sobe normalmente, do
                       mesmo jeito que qualquer outra falha do pipeline.
                       Pra voltar a usar o Claude, apague essa variável)
  DEEPSEEK_MODEL      (opcional, padrão: deepseek-v4-flash — os nomes
                      antigos deepseek-chat/deepseek-reasoner saem de linha
                      em 24/07/2026)
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import sys
import urllib.request

try:
    import tomllib  # stdlib a partir do Python 3.11 (o workflow usa 3.12)
except ModuleNotFoundError:  # pragma: no cover - só acontece em Python <3.11
    import tomli as tomllib  # type: ignore[no-redef]

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import unescape as html_unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse

import feedparser
import trafilatura
import yaml
from PIL import Image
from slugify import slugify

import cards as cards_module

# Sem isso, quando a saída não é um terminal (ex.: workflow do GitHub
# Actions, ou saída redirecionada/pipe), o Python bufferiza stdout em
# blocos grandes mas deixa stderr sem buffer — na prática, os
# print(..., file=sys.stderr) (avisos, erros) aparecem no log ANTES das
# mensagens de stdout que na verdade rodaram primeiro, o que confunde
# bastante na hora de debugar (ex.: um aviso parecendo ter acontecido
# antes da coleta de manchetes começar). Força stdout a também ser
# line-buffered, igual ao stderr, pra sair na ordem cronológica real.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

ROOT = Path(__file__).resolve().parent.parent
SOURCES_FILE = ROOT / "pipeline" / "sources.yaml"
SEEN_FILE = ROOT / "pipeline" / "seen.json"
POSTS_DIR = ROOT / "site" / "content" / "posts"
CARDS_CONTENT_DIR = ROOT / "site" / "content" / "cards"

MODEL = os.environ.get("MODEL", "claude-haiku-4-5-20251001")
FACTCHECK_MODEL = os.environ.get("FACTCHECK_MODEL", "claude-haiku-4-5-20251001")
CLUSTER_MODEL = os.environ.get("CLUSTER_MODEL", "claude-haiku-4-5-20251001")
CARDS_MODEL = os.environ.get("CARDS_MODEL", "claude-haiku-4-5-20251001")
MAX_PER_FEED = int(os.environ.get("MAX_PER_FEED", "4"))
MAX_SOURCE_CHARS = int(os.environ.get("MAX_SOURCE_CHARS", "6000"))
RECENT_CONTEXT_WINDOW_HOURS = int(os.environ.get("RECENT_CONTEXT_WINDOW_HOURS", "72"))
RECENT_CONTEXT_MAX_ITEMS = int(os.environ.get("RECENT_CONTEXT_MAX_ITEMS", "60"))
DRY_RUN = os.environ.get("DRY_RUN") == "1"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_IMAGE_MODEL = os.environ.get("GEMINI_IMAGE_MODEL", "gemini-2.5-flash-image")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# Conta qual provedor respondeu de fato cada uma das 3 chamadas de texto
# (agrupar/escrever/revisar) nesta execução — impresso no resumo final.
# Como DeepSeek e Claude são mutuamente exclusivos (ver call_llm), no
# máximo um dos dois contadores fica diferente de zero numa mesma rodada.
LLM_CALL_STATS = {"deepseek_ok": 0, "anthropic_ok": 0}


def active_text_model(anthropic_model: str) -> str:
    """Nome do modelo que vai de fato responder a uma chamada de texto
    (agrupar/escrever/revisar): DEEPSEEK_MODEL se DEEPSEEK_API_KEY estiver
    configurada (Claude não é chamado nesse caso — ver call_llm), senão o
    modelo Claude passado (CLUSTER_MODEL/MODEL/FACTCHECK_MODEL). Usado só
    pros prints de log ficarem corretos — sem isso, o log mostrava sempre
    o nome do modelo Claude configurado mesmo quando quem respondia de
    fato era o DeepSeek, o que parecia (incorretamente) que o Claude
    ainda estava em uso."""
    return DEEPSEEK_MODEL if DEEPSEEK_API_KEY else anthropic_model

# Categorias que o portal cobre. Qualquer manchete fora disso é descartada
# na etapa de classificação (não é gerada matéria). F4 foi removida do
# escopo ativo — matérias antigas dessa categoria continuam publicadas
# (não foram apagadas), só não entram mais matérias novas nela.
ALLOWED_CATEGORIES = ["Kart", "F1", "F2", "F3", "GT3", "WEC", "Indy", "NASCAR"]

# Sentinela que rewrite_with_claude devolve em "titulo" quando os
# textos-fonte não têm fato suficiente para uma matéria (ex.: página só com
# navegação/menu do site, sem conteúdo jornalístico). Detectado em main()
# para NUNCA virar post — antes disso o pipeline publicava uma "matéria"
# só com uma nota dizendo que o conteúdo era insuficiente, o que não devia
# acontecer.
INSUFFICIENT_CONTENT_TITLE = "CONTEUDO_INSUFICIENTE"


# --------------------------------------------------------------------------
# Escritores (pseudônimos editoriais)
# --------------------------------------------------------------------------
#
# O BRGrid publica sob pseudônimos fixos em vez do nome de uma pessoa real.
# Cada "escritor" tem sua própria voz/persona (usada na geração da matéria,
# na revisão de fatos — que precisa PRESERVAR o tom de quem escreveu — e na
# geração dos cards de Stories) e um critério de quando é escolhido pra
# assinar uma matéria.
#
# Pra adicionar um novo estilo de editor no futuro: crie um novo
# WriterProfile (author_name, persona, tone_note, min_sources) e insira-o
# em WRITER_PROFILES na posição certa (ver comentário na lista). Nenhum
# outro código precisa mudar — build_write_system_prompt/
# build_factcheck_system_prompt/build_cards_system_prompt e select_writer()
# já trabalham genericamente com qualquer WriterProfile.

@dataclass(frozen=True)
class WriterProfile:
    key: str
    author_name: str
    persona: str        # bloco "Você é um editor..." que descreve o estilo
    tone_note: str       # frase curta descrevendo o tom, usada no factcheck/cards
    min_sources: int = 0  # nº mínimo de textos-fonte utilizáveis pra ser escolhido


BRUNO_BANDEIRA = WriterProfile(
    key="bruno_bandeira",
    author_name="Bruno Bandeira",
    persona="""Você é um editor de notícias experiente, com olhar crítico e senso de humor
afiado. Sua missão é transformar textos jornalísticos em conteúdos mais
envolventes, adicionando comentários sarcásticos, ironia elegante e toques
de humor, sem alterar os fatos ou comprometer a credibilidade da
informação. Seu estilo deve: preservar a precisão dos acontecimentos e dos
dados apresentados. Usar sarcasmo inteligente para destacar contradições,
exageros e situações curiosas. Inserir humor de forma natural, com piadas
rápidas, analogias criativas e observações espirituosas. Escrever de
maneira clara, dinâmica e agradável de ler. Criar manchetes e subtítulos
chamativos, bem-humorados e memoráveis quando solicitado. Evitar o humor
ofensivo, difamatório ou baseado em ataques pessoais; a piada deve ser da
situação, não das pessoas. O resultado deve parecer uma reportagem escrita
por um jornalista experiente que sabe informar com precisão, mas não perde
a oportunidade de arrancar um sorriso do leitor com uma ironia bem
colocada. Sempre com precisão em relação aos fatos.""",
    tone_note="irônico e bem-humorado",
    min_sources=0,
)

ARMANDO_TRACO = WriterProfile(
    key="armando_traco",
    author_name="Armando Traço",
    persona="""Você é um editor de notícias experiente e com sólida bagagem técnica em
automobilismo, com foco em precisão, profundidade e clareza. Sua missão é
transformar textos jornalísticos em matérias mais completas e tecnicamente
rigorosas, priorizando os detalhes de engenharia, estratégia, regulamento
e desempenho por trás do fato. Seu estilo deve: preservar a precisão dos
acontecimentos e dos dados apresentados, com atenção redobrada a números,
especificações e termos técnicos. Explicar o "porquê" técnico por trás dos
fatos (setup, estratégia de pneus e combustível, regulamento, aerodinâmica
etc.) sempre que os textos-fonte permitirem. Escrever de maneira objetiva,
direta e bem estruturada, aprofundando cada aspecto relevante da notícia
em vez de só descrevê-lo por cima. Usar humor e ironia com MUITA
moderação — no máximo um toque discreto e ocasional, nunca como elemento
central do texto. Criar manchetes e subtítulos informativos e diretos, sem
apelo humorístico. O resultado deve parecer uma reportagem escrita por um
analista técnico experiente, que prioriza informar com profundidade e
precisão acima de entreter. Sempre com precisão em relação aos fatos.""",
    tone_note="técnico, objetivo e com humor bem discreto",
    min_sources=3,
)

# Ordenada do critério MAIS exigente pro MAIS geral — o último da lista
# deve sempre ter min_sources=0 (é o "coringa"/fallback de select_writer).
# Pra inserir um novo escritor entre os dois (ex.: min_sources=1 ou 2),
# basta colocá-lo na posição correspondente nessa lista.
WRITER_PROFILES: list[WriterProfile] = [
    ARMANDO_TRACO,
    BRUNO_BANDEIRA,
]

# Mantido por compatibilidade (usado em textos/README como "o pseudônimo
# padrão do site") — o autor de CADA matéria, na prática, vem de
# select_writer(), não dessa constante.
AUTHOR_NAME = BRUNO_BANDEIRA.author_name


def select_writer(usable_source_count: int) -> WriterProfile:
    """Escolhe qual escritor assina a matéria, com base em quantos
    textos-fonte UTILIZÁVEIS o grupo tem (não a quantidade bruta de
    manchetes agrupadas) — o que importa é quanto material de verdade dá
    pra trabalhar, já que é isso que permite (ou não) uma matéria mais
    longa e tecnicamente detalhada. Percorre WRITER_PROFILES em ordem e
    devolve o primeiro escritor cujo mínimo de fontes é atendido; como o
    último da lista tem min_sources=0, sempre existe um fallback (Bruno
    Bandeira, pro caso de poucas fontes)."""
    for writer in WRITER_PROFILES:
        if usable_source_count >= writer.min_sources:
            return writer
    return WRITER_PROFILES[-1]  # nunca deveria chegar aqui


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
   (Kart = kartismo em qualquer lugar do mundo; F1/F2/F3 = categorias da
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
6. Você também vai receber, à parte, uma lista de MATÉRIAS JÁ PUBLICADAS
   pelo próprio site recentemente (título, categoria, há quanto tempo).
   Use isso como CONTEXTO pra não publicar nada redundante ou já
   superado: se uma manchete nova cobre uma etapa ANTERIOR do MESMO
   evento/fato que uma matéria já publicada (mais recente e mais
   avançada) já cobre — por exemplo, uma manchete sobre o resultado da
   CLASSIFICAÇÃO quando já publicamos o resultado da CORRIDA daquela
   mesma etapa, ou uma manchete de treino livre quando já publicamos a
   classificação daquela mesma etapa — marque o grupo dela como
   "DESCARTAR": a informação já está desatualizada e não agrega nada ao
   leitor que já viu a notícia mais recente. Só descarte por esse motivo
   quando der pra confirmar que é o MESMO evento (mesmo circuito/etapa/
   categoria) numa fase mais avançada — na dúvida, não descarte.

Responda APENAS com um objeto JSON válido (sem markdown, sem crases):
{{
  "grupos": [
    {{"ids": [0, 3], "categoria": "F1"}},
    {{"ids": [1], "categoria": "DESCARTAR"}}
  ]
}}"""

def build_write_system_prompt(writer: WriterProfile) -> str:
    return f"""\
Você é {writer.author_name}, editor do BRGrid, portal brasileiro de notícias
de automobilismo focado em: {", ".join(ALLOWED_CATEGORIES)}.

{writer.persona}

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
- Se o material for insuficiente para uma matéria de verdade (ex.: os
  textos-fonte são só navegação/menu do site, cookie banner, ou não têm
  nenhum fato jornalístico de automobilismo), NÃO escreva uma nota
  explicando que faltou conteúdo — isso não deve virar matéria publicada.
  Em vez disso, responda com "titulo": "{INSUFFICIENT_CONTENT_TITLE}" e os
  demais campos vazios ("linha_fina": "", "tags": [], "corpo_markdown": "").
  O sistema descarta essa resposta automaticamente.
- A(s) fonte(s) será(ão) creditada(s) pelo sistema; você não precisa citá-las.
- IMPORTANTE (formatação do JSON): para citação ou ênfase, prefira aspas
  simples (‘assim’) em vez de aspas duplas — evita quebrar o JSON. Se
  ainda assim precisar usar aspas duplas DENTRO de um valor de string,
  escape cada uma delas como \" (barra invertida antes da aspas). Nunca
  deixe uma aspas dupla sem escapar dentro de um valor — isso invalida o
  JSON inteiro e a matéria é perdida.

Responda APENAS com um objeto JSON válido (sem markdown, sem crases), no formato:
{{
  "titulo": "título curto e chamativo, no tom descrito acima (ou \"{INSUFFICIENT_CONTENT_TITLE}\" se o material for insuficiente, ver regra acima)",
  "linha_fina": "uma frase de resumo (o 'dek'), também no mesmo tom",
  "categoria": "uma de: {", ".join(ALLOWED_CATEGORIES)}",
  "tags": ["até 4 tags curtas"],
  "corpo_markdown": "3 a 5 parágrafos em Markdown"
}}"""

def build_factcheck_system_prompt(writer: WriterProfile) -> str:
    return f"""\
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
- O rascunho é escrito no tom {writer.tone_note} (voz editorial de
  {writer.author_name}) — PRESERVE esse tom e a fluidez; corrija o mínimo
  necessário para garantir precisão, sem reescrever o texto do zero e sem
  descaracterizar o estilo ao corrigir um dado. Ajuste só o dado em si
  (nome, número, resultado etc.), mantendo o restante do texto como está
  sempre que possível.
- Se o rascunho já estiver correto, devolva-o sem alterações.
- Se, depois de remover/generalizar tudo que não é verificável, sobrar pouco
  ou nenhum fato de verdade (o rascunho vira só generalidades vagas, sem
  nenhuma informação concreta sobre o evento), NÃO devolva esse resultado
  fraco como matéria. Em vez disso, responda com
  "titulo": "{INSUFFICIENT_CONTENT_TITLE}" e os demais campos vazios
  ("linha_fina": "", "tags": [], "corpo_markdown": ""), do mesmo jeito que o
  editor faz quando o material de origem já vem insuficiente.
- IMPORTANTE (formatação do JSON): se usar aspas duplas dentro de um valor
  de string (para citação ou ênfase), escape cada uma delas como \"
  (barra invertida antes da aspas) — nunca deixe uma aspas dupla sem
  escapar dentro de um valor, isso invalida o JSON e a matéria é perdida.
  Prefira aspas simples (‘assim’) quando possível.

Responda APENAS com um objeto JSON válido (sem markdown, sem crases), EXATAMENTE
no mesmo formato do rascunho recebido:
{{
  "titulo": "...",
  "linha_fina": "...",
  "categoria": "...",
  "tags": ["..."],
  "corpo_markdown": "..."
}}"""


def build_cards_system_prompt(writer: WriterProfile) -> str:
    return f"""\
Você é {writer.author_name}, editor do BRGrid, adaptando uma matéria já
pronta pra uma sequência de "cards" de Stories (Instagram) — imagens
verticais, uma frase de cada vez, que as pessoas passam o dedo pra ler
rapidinho.

Vai receber a matéria final (título, linha fina, corpo) em JSON. Decida
quantos cards fazem sentido — ENTRE 1 E 5 — de acordo com o quanto a
matéria realmente tem de conteúdo interessante:
- Notícia simples/curta (pouco mais que um fato só): 1 ou 2 cards bastam.
- Matéria rica em detalhes (vários fatos, contexto, resultado + reação
  etc.): pode ir até 5. NÃO force 5 cards enchendo linguiça — cada card
  tem que carregar uma ideia de verdade, não repetir a mesma coisa com
  outras palavras.

Regras de cada card:
- Uma frase curta, ou no máximo duas bem curtas — pensado pra caber numa
  tela de celular, sem parágrafo. Nada de explicação longa.
- Baseie-se SÓ nos fatos que já estão na matéria recebida — não invente
  nada novo, não adicione números/nomes que não estejam lá.
- O primeiro card é o gancho principal (pode adaptar o próprio título da
  matéria, deixando mais direto pro formato Stories).
- Cards do meio (se houver) trazem outros fatos/detalhes que valem
  destaque — um por card, sem repetir o gancho.
- O card final fecha convidando a ler a matéria completa (o link/handle
  do site já é adicionado automaticamente por fora — você só escreve a
  frase de fechamento, tipo uma "deixa no ar").
- Mantenha o tom {writer.tone_note} de {writer.author_name}, mas adaptado
  ao formato rápido de Stories: mais direto, menos elaborado que o corpo
  da matéria completa.
- IMPORTANTE (formatação do JSON): prefira aspas simples (‘assim’) pra
  citação ou ênfase. Se usar aspas duplas dentro de um card, escape cada
  uma como \" — aspas dupla sem escapar quebra o JSON e os cards são
  perdidos (a matéria continua publicada normalmente, só sem cards).

Responda APENAS com um objeto JSON válido (sem markdown, sem crases), no formato:
{{
  "cards": ["primeiro card", "segundo card", "... até 5"]
}}"""


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
# Contexto: matérias já publicadas (pra não gerar notícia redundante/já
# superada — ex.: publicar a classificação depois que a corrida já saiu)
# --------------------------------------------------------------------------

_FRONTMATTER_PATTERN = re.compile(r"^\+\+\+\n(.*?)\n\+\+\+\n", re.S)


def _read_post_frontmatter(path: Path) -> dict | None:
    """Lê e devolve o frontmatter TOML (entre os delimitadores +++) de um
    arquivo de matéria já publicada em site/content/posts/. Devolve None se
    não conseguir ler/parsear — um post com formato inesperado não deve
    derrubar a coleta de contexto, só é ignorado."""
    try:
        text = path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return None
    match = _FRONTMATTER_PATTERN.match(text)
    if not match:
        return None
    try:
        return tomllib.loads(match.group(1))
    except Exception:  # noqa: BLE001
        return None


def load_recent_published_context(
    now: datetime,
    window_hours: int = RECENT_CONTEXT_WINDOW_HOURS,
    max_items: int = RECENT_CONTEXT_MAX_ITEMS,
) -> list[dict]:
    """Lê as matérias já publicadas em site/content/posts/ dentro da janela
    de tempo recente, pra servir de CONTEXTO no agrupamento/classificação
    (ver CLUSTER_SYSTEM_PROMPT, regra 6): o modelo precisa saber o que o
    site JÁ publicou sobre um evento pra não gerar uma matéria redundante
    ou já superada (ex.: publicar a classificação de uma etapa depois que
    o resultado da corrida daquela mesma etapa já saiu). Devolve uma lista
    de {"titulo", "categoria", "horas_atras"}, mais recentes primeiro,
    limitada a `max_items` pra não estourar o tamanho do prompt em rodadas
    com muito histórico publicado."""
    if not POSTS_DIR.exists():
        return []
    cutoff = now - timedelta(hours=window_hours)
    items = []
    for path in POSTS_DIR.glob("*.md"):
        fm = _read_post_frontmatter(path)
        if not fm:
            continue
        date_raw = fm.get("date")
        date = date_raw if isinstance(date_raw, datetime) else None
        if date is None and date_raw:
            try:
                date = datetime.fromisoformat(str(date_raw))
            except Exception:  # noqa: BLE001
                continue
        if date is None:
            continue
        if date.tzinfo is None:
            date = date.replace(tzinfo=timezone.utc)
        if date < cutoff:
            continue
        categories = fm.get("categories") or []
        items.append(
            {
                "titulo": fm.get("title", ""),
                "categoria": categories[0] if categories else "",
                "date": date,
            }
        )
    items.sort(key=lambda it: it["date"], reverse=True)
    items = items[:max_items]
    for it in items:
        horas = max(0, round((now - it["date"]).total_seconds() / 3600))
        it["horas_atras"] = horas
        del it["date"]
    return items


# --------------------------------------------------------------------------
# Coleta de candidatos (RSS ou listagem HTML)
# --------------------------------------------------------------------------

# Se a data que vem da fonte (RSS ou extraída por trafilatura da página)
# for mais velha que MAX_SOURCE_DATE_AGE_DAYS, o candidato é DESCARTADO —
# não vira matéria. Caso real: uma página "evergreen" de catálogo de
# produto (ex.: TKART) apareceu pela primeira vez na listagem — link nunca
# visto antes, então o pipeline trataria como notícia nova — mas o
# conteúdo em si tinha data de 2022/2023. Usar a data original faria o
# post nascer "enterrado" no fim da lista cronológica do site; usar a
# data de hoje seria publicar como notícia algo que não é mais notícia.
# A regra é simples: usamos SEMPRE a data original da fonte, e se ela for
# velha demais, simplesmente não publicamos.
MAX_SOURCE_DATE_AGE_DAYS = 30


def is_source_date_stale(date: datetime) -> bool:
    """True se `date` (data original extraída da fonte) for velha demais
    pra ainda contar como notícia — ver comentário acima."""
    return date < datetime.now(timezone.utc) - timedelta(days=MAX_SOURCE_DATE_AGE_DAYS)


def entry_date_from_struct(struct_time) -> datetime:
    if struct_time:
        return datetime(*struct_time[:6], tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _empty_collect_stats() -> dict:
    """Contadores devolvidos por collect_rss_candidates/collect_list_candidates
    junto com a lista de candidatos — usados por collect_all_candidates pra
    imprimir um raio-x detalhado do que aconteceu em CADA fonte (pedido do
    dono do projeto: visão completa de quantas manchetes foram lidas, quantas
    já eram conhecidas, quantas foram descartadas e por quê, etc.)."""
    return {
        "lidas": 0,
        "ja_conhecidas": 0,
        "falha_download_ou_extracao": 0,
        "texto_curto": 0,
        "descartadas_data": 0,
        "nao_processadas_por_limite": 0,
        "novas": 0,
    }


def collect_rss_candidates(feed_cfg: dict, seen: set[str]) -> tuple[list[dict], dict]:
    name, url = feed_cfg["name"], feed_cfg["url"]
    extra_instructions = feed_cfg.get("extra_instructions")
    stats = _empty_collect_stats()
    try:
        parsed = feedparser.parse(url)
    except Exception as exc:  # noqa: BLE001
        print(f"  erro ao ler o feed: {exc}", file=sys.stderr)
        return [], stats
    if parsed.bozo and not parsed.entries:
        print(f"  feed vazio ou inválido ({url})", file=sys.stderr)
        return [], stats

    stats["lidas"] = len(parsed.entries)
    out = []
    for i, entry in enumerate(parsed.entries):
        if len(out) >= MAX_PER_FEED:
            stats["nao_processadas_por_limite"] = len(parsed.entries) - i
            break
        link = entry.get("link")
        if not link:
            continue
        if link in seen:
            stats["ja_conhecidas"] += 1
            continue
        title = entry.get("title", "Sem título")
        summary = entry.get("summary", "") or entry.get("description", "")
        date = entry_date_from_struct(
            entry.get("published_parsed") or entry.get("updated_parsed")
        )
        if is_source_date_stale(date):
            stats["descartadas_data"] += 1
            print(f"    descartado (>{MAX_SOURCE_DATE_AGE_DAYS}d, {date.date()}): {title[:70]}")
            seen.add(link)
            continue
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
    stats["novas"] = len(out)
    return out, stats


def collect_list_candidates(feed_cfg: dict, seen: set[str]) -> tuple[list[dict], dict]:
    """Para sites sem RSS: lê a página de listagem e extrai links de matéria."""
    name = feed_cfg["name"]
    list_url = feed_cfg["list_url"]
    link_contains = feed_cfg["link_contains"]
    extra_instructions = feed_cfg.get("extra_instructions")
    stats = _empty_collect_stats()

    try:
        downloaded = trafilatura.fetch_url(list_url)
    except Exception as exc:  # noqa: BLE001
        print(f"  erro ao baixar listagem ({exc})", file=sys.stderr)
        return [], stats
    if not downloaded:
        print(f"  listagem vazia ({list_url})", file=sys.stderr)
        return [], stats

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
    stats["lidas"] = len(links)

    out = []
    for i, link in enumerate(links):
        if len(out) >= MAX_PER_FEED:
            stats["nao_processadas_por_limite"] = len(links) - i
            break
        if link in seen:
            stats["ja_conhecidas"] += 1
            continue
        try:
            article_html = trafilatura.fetch_url(link)
            if not article_html:
                stats["falha_download_ou_extracao"] += 1
                continue
            data = trafilatura.bare_extraction(article_html, with_metadata=True)
        except Exception as exc:  # noqa: BLE001
            print(f"    aviso: falha ao ler {link} ({exc})", file=sys.stderr)
            stats["falha_download_ou_extracao"] += 1
            continue
        if not data:
            stats["falha_download_ou_extracao"] += 1
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
            stats["texto_curto"] += 1
            continue
        title = title or link
        date = datetime.now(timezone.utc)
        if raw_date:
            try:
                date = datetime.fromisoformat(str(raw_date)).replace(tzinfo=timezone.utc)
            except Exception:  # noqa: BLE001
                pass
        if is_source_date_stale(date):
            stats["descartadas_data"] += 1
            print(f"    descartado (>{MAX_SOURCE_DATE_AGE_DAYS}d, {date.date()}): {title[:70]}")
            seen.add(link)
            continue
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
    stats["novas"] = len(out)
    return out, stats


def collect_all_candidates(feeds: list[dict], seen: set[str]) -> list[dict]:
    """Coleta manchetes de todas as fontes, imprimindo um raio-x detalhado
    de cada uma (quantas foram lidas, quantas já eram conhecidas, quantas
    foram descartadas e por quê, quantas ficaram de fora por causa do
    limite MAX_PER_FEED) e um resumo agregado no final."""
    candidates = []
    totals = _empty_collect_stats()
    for feed_cfg in feeds:
        is_list = feed_cfg.get("type") == "list"
        print(f"\n== {feed_cfg['name']} ({'raspagem de listagem' if is_list else 'RSS'}) ==")
        if is_list:
            found, stats = collect_list_candidates(feed_cfg, seen)
        else:
            found, stats = collect_rss_candidates(feed_cfg, seen)

        rotulo_lidas = "links encontrados na listagem" if is_list else "manchetes no feed"
        print(f"  {rotulo_lidas}:              {stats['lidas']}")
        print(f"  já conhecidas (seen.json):   {stats['ja_conhecidas']}")
        if is_list:
            print(f"  falha ao baixar/extrair:     {stats['falha_download_ou_extracao']}")
            print(f"  texto curto demais (<120c):  {stats['texto_curto']}")
        print(f"  descartadas (data antiga):   {stats['descartadas_data']}")
        if stats["nao_processadas_por_limite"]:
            print(
                f"  não processadas (limite de {MAX_PER_FEED}/rodada atingido): "
                f"{stats['nao_processadas_por_limite']}"
            )
        print(f"  → novas nesta rodada:        {stats['novas']}")

        candidates.extend(found)
        for key in totals:
            totals[key] += stats[key]

    print("\n" + "-" * 60)
    print("Resumo da coleta (todas as fontes)")
    print("-" * 60)
    print(f"Lidas no total (feeds + listagens):     {totals['lidas']}")
    print(f"Já conhecidas (seen.json):               {totals['ja_conhecidas']}")
    print(f"Falha ao baixar/extrair:                 {totals['falha_download_ou_extracao']}")
    print(f"Texto curto demais:                      {totals['texto_curto']}")
    print(f"Descartadas (data antiga):                {totals['descartadas_data']}")
    print(f"Não processadas (limite por fonte):      {totals['nao_processadas_por_limite']}")
    print(f"Novas (candidatas à 2ª fase):             {totals['novas']}")
    return candidates


# --------------------------------------------------------------------------
# Parsing de JSON tolerante
# --------------------------------------------------------------------------

# Campos das respostas de rewrite_with_claude/factcheck_with_claude, na
# ordem em que são pedidos nos prompts. Usado só pelo reparo tolerante
# abaixo (_lenient_json_repair) — a resposta de cluster_and_classify usa
# outro esquema ("grupos") e não passa por esse fallback.
_ARTICLE_FIELD_ORDER = ["titulo", "linha_fina", "categoria", "tags", "corpo_markdown"]


def _lenient_json_repair(cleaned: str) -> dict:
    """Fallback para quando json.loads falha ao parsear a resposta do
    modelo. Causa mais comum: o texto usa aspas duplas "irônicas" dentro
    de um valor string (titulo/linha_fina/corpo_markdown) sem escapá-las,
    quebrando a sintaxe do JSON ("Expecting ',' delimiter",
    "Unterminated string", etc.) — mais frequente desde que o tom editorial
    ficou mais sarcástico/citação-pesado.

    Como o formato é sempre um objeto plano com as mesmas chaves na mesma
    ordem (ver _ARTICLE_FIELD_ORDER), localizamos cada "chave": no texto
    bruto e tratamos tudo até a próxima chave (ou o fim do objeto) como o
    valor daquele campo — cortando a aspas de abertura e a ÚLTIMA aspas
    restante no trecho (em vez da primeira, que é o que quebra o parser
    JSON oficial quando há aspas internas não escapadas no meio do valor).
    Não é um parser de JSON genérico; é deliberadamente específico a este
    esquema fixo."""
    positions = []
    for key in _ARTICLE_FIELD_ORDER:
        m = re.search(rf'"{key}"\s*:\s*', cleaned)
        if m:
            positions.append((key, m.start(), m.end()))
    if not positions:
        raise ValueError("nenhuma chave conhecida (titulo/corpo_markdown/...) encontrada")
    positions.sort(key=lambda p: p[1])

    result: dict = {}
    for i, (key, _start, val_start) in enumerate(positions):
        chunk_end = positions[i + 1][1] if i + 1 < len(positions) else len(cleaned)
        chunk = cleaned[val_start:chunk_end]

        if key == "tags":
            result[key] = re.findall(r'"([^"]*)"', chunk)
            continue

        chunk = chunk.strip()
        if chunk.startswith('"'):
            chunk = chunk[1:]
        last_quote = chunk.rfind('"')
        chunk = chunk[:last_quote] if last_quote != -1 else chunk.rstrip().rstrip(",").rstrip("}").strip()
        chunk = chunk.replace('\\"', '"').replace("\\n", "\n").replace("\\\\", "\\")
        result[key] = chunk

    if not result.get("titulo") or not result.get("corpo_markdown"):
        raise ValueError("reparo não encontrou titulo/corpo_markdown utilizáveis")
    return result


def parse_model_json(raw: str) -> dict:
    """Extrai o JSON da resposta do modelo, tolerando crases ou texto extra.
    Se o JSON estiver malformado (tipicamente aspas internas não escapadas),
    tenta um reparo tolerante antes de desistir — ver _lenient_json_repair."""
    cleaned = raw.strip()
    cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
    cleaned = re.sub(r"```$", "", cleaned).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end != -1:
        cleaned = cleaned[start : end + 1]
    try:
        return json.loads(cleaned, strict=False)
    except json.JSONDecodeError as exc:
        try:
            repaired = _lenient_json_repair(cleaned)
        except Exception:  # noqa: BLE001
            raise exc from None  # erro original é mais informativo se o reparo também falhar
        print(f"    aviso: JSON malformado ({exc}), recuperado via reparo tolerante", file=sys.stderr)
        return repaired


def is_insufficient_content(article: dict) -> bool:
    """Detecta se rewrite_with_claude/factcheck_with_claude sinalizaram que
    o material não dá pra uma matéria de verdade (ver INSUFFICIENT_CONTENT_TITLE
    nos prompts). Checado depois das DUAS chamadas — rewrite pode sinalizar
    de cara, e factcheck pode chegar à mesma conclusão depois de remover
    tudo que não é verificável do rascunho. Usa também um fallback por
    substring ("insuficiente" no título) para o caso do modelo não seguir a
    instrução da sentinela à risca."""
    titulo_normalizado = (article.get("titulo") or "").strip().casefold()
    return (
        titulo_normalizado == INSUFFICIENT_CONTENT_TITLE.casefold()
        or "insuficiente" in titulo_normalizado
    )


# --------------------------------------------------------------------------
# Chamadas ao LLM: DeepSeek (se configurado) OU Claude/Anthropic —
# mutuamente exclusivos, sem fallback entre os dois
# --------------------------------------------------------------------------
#
# call_llm() é o único ponto por onde as 3 etapas de texto (agrupar,
# escrever, checar fatos) falam com um modelo de linguagem. Se
# DEEPSEEK_API_KEY estiver definida, usa SÓ o DeepSeek — se a chamada
# falhar (rede, HTTP, quota, resposta vazia), o erro sobe pra quem chamou
# em vez de cair pro Claude. Pra voltar a usar o Claude, é só apagar a
# variável DEEPSEEK_API_KEY.

def _call_deepseek(system: str, user_content: str, max_tokens: int) -> str:
    # Todo texto que a gente pede pro DeepSeek é JSON (ver os *_SYSTEM_PROMPT
    # acima, todos terminam pedindo "responda APENAS com um objeto JSON
    # válido"). Sem o response_format abaixo, o modelo às vezes acrescenta
    # texto fora do JSON ou corta a string no meio de uma aspas — foi a causa
    # dos erros de "JSON malformado"/falha na revisão de fatos que já vimos
    # em produção. O JSON Output nativo do DeepSeek reduz bastante isso (as
    # duas exigências da própria doc — response_format e a palavra "json" +
    # exemplo de formato no prompt — já estão cobertas: os prompts já pedem
    # "objeto JSON válido" e mostram o formato esperado).
    body = json.dumps(
        {
            "model": DEEPSEEK_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user_content},
            ],
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        f"{DEEPSEEK_BASE_URL}/chat/completions",
        data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:  # noqa: BLE001
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    choice = payload["choices"][0]
    content = (choice.get("message") or {}).get("content")
    if not content or not content.strip():
        # A própria doc do DeepSeek avisa que o JSON Output pode ocasionalmente
        # devolver conteúdo vazio (bug conhecido deles, "actively working on
        # optimizing"). Não tem workaround do nosso lado além de deixar subir
        # como falha — quem chama já trata isso como não-fatal.
        raise RuntimeError(f"resposta vazia (finish_reason={choice.get('finish_reason')})")
    return content


def _call_anthropic(system: str, user_content: str, max_tokens: int, model: str) -> str:
    from anthropic import Anthropic

    client = Anthropic()
    message = client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user_content}],
    )
    return "".join(block.text for block in message.content if block.type == "text")


def call_llm(system: str, user_content: str, max_tokens: int, anthropic_model: str) -> str:
    """Se DEEPSEEK_API_KEY estiver configurada, usa SOMENTE o DeepSeek —
    NÃO cai pro Claude/Anthropic se o DeepSeek falhar (rede, quota,
    resposta vazia/inválida etc.); o erro sobe pra quem chamou, do mesmo
    jeito que qualquer outro erro do pipeline (rede, parsing...) já é
    tratado (ver o try/except em torno de cada etapa em main()). Pra
    voltar a usar o Claude, apague a variável DEEPSEEK_API_KEY — sem ela,
    usa Claude/Anthropic normalmente, como sempre."""
    if DEEPSEEK_API_KEY:
        result = _call_deepseek(system, user_content, max_tokens)
        LLM_CALL_STATS["deepseek_ok"] += 1
        return result
    result = _call_anthropic(system, user_content, max_tokens, anthropic_model)
    LLM_CALL_STATS["anthropic_ok"] += 1
    return result


def cluster_and_classify(candidates: list[dict], recent_context: list[dict] | None = None) -> list[dict]:
    """Agrupa candidatos pelo mesmo fato e classifica cada grupo.

    `recent_context` (ver load_recent_published_context) é a lista de
    matérias já publicadas recentemente pelo site — passada como um bloco
    à parte no início do prompt, pra o modelo aplicar a regra 6 do
    CLUSTER_SYSTEM_PROMPT (não publicar nada redundante/já superado por
    notícia mais recente do mesmo evento)."""
    if DRY_RUN:
        return [{"ids": [i], "categoria": ALLOWED_CATEGORIES[0]} for i in range(len(candidates))]

    blocks = []
    if recent_context:
        context_lines = [
            f"- [{it['categoria']}] {it['titulo']} (publicada há {it['horas_atras']}h)"
            for it in recent_context
        ]
        blocks.append(
            "MATÉRIAS JÁ PUBLICADAS PELO SITE RECENTEMENTE (contexto — ver regra 6):\n"
            + "\n".join(context_lines)
        )

    lines = []
    for i, c in enumerate(candidates):
        summary = (c.get("summary") or "")[:220].replace("\n", " ")
        line = f"id {i} | fonte: {c['name']} | título: {c['title']} | resumo: {summary}"
        if c.get("extra_instructions"):
            line += f" | regra da fonte: {c['extra_instructions']}"
        lines.append(line)
    blocks.append("MANCHETES NOVAS DESTA RODADA (agrupar/classificar):\n" + "\n".join(lines))
    user_content = "\n\n---\n\n".join(blocks)

    raw = call_llm(CLUSTER_SYSTEM_PROMPT, user_content, 4096, CLUSTER_MODEL)
    try:
        data = parse_model_json(raw)
    except Exception as exc:  # noqa: BLE001
        print(f"    aviso: falha ao parsear resposta do agrupamento: {exc}", file=sys.stderr)
        print(f"    resposta bruta ({len(raw)} chars): {raw[:500]!r}", file=sys.stderr)
        raise
    return data.get("grupos", [])


def rewrite_with_claude(
    source_blocks: list[tuple[str, str, str | None]], writer: WriterProfile
) -> dict:
    """source_blocks: lista de (nome_da_fonte, texto, instrução_extra). Gera
    UMA matéria na voz do `writer` escolhido (ver select_writer())."""
    if DRY_RUN:
        names = ", ".join(n for n, _, _ in source_blocks)
        return {
            "titulo": f"[TESTE:{writer.author_name}] Matéria de {names}",
            "linha_fina": "Matéria de teste gerada em modo DRY_RUN.",
            "categoria": ALLOWED_CATEGORIES[0],
            "tags": ["teste"],
            "corpo_markdown": (
                "Este é um texto de teste gerado sem chamar a API.\n\n"
                "Defina `DRY_RUN=0` e a variável `ANTHROPIC_API_KEY` para gerar de verdade."
            ),
        }

    parts = []
    for name, text, extra_instructions in source_blocks:
        header = f"[Fonte: {name}]"
        if extra_instructions:
            header += f" [Instrução especial para esta fonte: {extra_instructions}]"
        parts.append(f"{header}\n{text[:MAX_SOURCE_CHARS]}")
    user_content = "\n\n---\n\n".join(parts)

    raw = call_llm(build_write_system_prompt(writer), user_content, 4096, MODEL)
    return parse_model_json(raw)


def factcheck_with_claude(
    article: dict, source_blocks: list[tuple[str, str, str | None]], writer: WriterProfile
) -> dict:
    """Segunda passada: revisa o rascunho contra os textos-fonte originais,
    preservando o tom do MESMO `writer` que escreveu o rascunho."""
    if DRY_RUN:
        return article

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
    raw = call_llm(build_factcheck_system_prompt(writer), user_content, 4096, FACTCHECK_MODEL)
    return parse_model_json(raw)


def generate_card_texts(article: dict, writer: WriterProfile) -> list[str]:
    """Pede ao modelo entre 1 e 5 frases curtas pra virarem cards de Stories,
    na voz do MESMO `writer` que escreveu a matéria (ver
    build_cards_system_prompt). Levanta exceção se a resposta vier vazia,
    malformada ou fora do esperado — quem chama trata isso como não-fatal
    (a matéria é publicada normalmente, só sem cards)."""
    if DRY_RUN:
        return [f"[TESTE:{writer.author_name}] {article.get('titulo', 'Matéria de teste')}"]

    user_content = json.dumps(
        {
            "titulo": article.get("titulo", ""),
            "linha_fina": article.get("linha_fina", ""),
            "categoria": article.get("categoria", ""),
            "corpo_markdown": article.get("corpo_markdown", ""),
        },
        ensure_ascii=False,
    )
    raw = call_llm(build_cards_system_prompt(writer), user_content, 1024, CARDS_MODEL)
    data = parse_model_json(raw)
    cards = [str(c).strip() for c in data.get("cards", []) if str(c).strip()]
    if not cards:
        raise ValueError("resposta não trouxe nenhum card utilizável")
    return cards[:5]


# --------------------------------------------------------------------------
# Imagens: variação por IA (Gemini "Nano Banana") a partir da imagem-fonte
# --------------------------------------------------------------------------
#
# Única fonte de imagem do site: usamos a própria imagem que ilustra a
# matéria no site de origem, baixamos essa imagem e pedimos a um modelo de
# geração/edição de imagem (Gemini, apelidado de "Nano Banana") para criar
# uma VARIAÇÃO dela — muda um pouco o ângulo das pessoas e dos carros
# visíveis, mas preserva o contexto geral da cena. A ideia é ter uma imagem
# com relação real ao fato noticiado, sem republicar a foto original de
# terceiros sem licença.
#
# Não há mais banco de fotos genérico como reserva (Unsplash/Pexels foram
# removidos de propósito). Se qualquer etapa falhar (matéria-fonte sem
# imagem, GEMINI_API_KEY não configurada, erro de rede/API), a função
# devolve None e a matéria fica sem imagem — o template usa o placeholder
# colorido por categoria nesse caso.

GENERATED_IMAGES_DIR = ROOT / "site" / "static" / "images" / "ia"
GENERATED_IMAGES_URL_PREFIX = "/images/ia"
GENERATED_CARDS_DIR = ROOT / "site" / "static" / "images" / "cards"
GENERATED_CARDS_URL_PREFIX = "/images/cards"

# Prompt fixo pedido pelo dono do projeto para a variação de imagem.
IMAGE_VARIATION_PROMPT = (
    "Crie uma variação dessa imagem, mudando um pouco o ângulo das pessoas "
    "e dos carros visíveis, de forma pronunciada mas que não altere o "
    "contexto geral. Também evitar trocar as cores."
)

# A API do Gemini cobra tokens proporcionalmente ao tamanho da imagem
# enviada (mais tiles = mais tokens) — e a imagem-fonte, direto do site
# original, normalmente vem em resolução bem mais alta do que a variação
# precisa. Reduzimos o tamanho ANTES de enviar pra geração, por dois
# motivos independentes: (1) corta a resolução em 40% (fica com 60% do
# tamanho original) — pedido explícito do dono do projeto, a qualidade da
# imagem gerada estava alta demais; (2) além disso, limita a MAIOR
# dimensão a um teto fixo, pra imagens-fonte enormes não voltarem a
# estourar o custo de token só por já começarem gigantes antes do corte
# de 40%.
IMAGE_GEN_RESIZE_SCALE = 0.6
IMAGE_GEN_MAX_DIMENSION = 1024


# Pedaços de URL que indicam que a "imagem" é na verdade um logo, ícone,
# avatar, sprite ou pixel de rastreamento — não uma foto de matéria. Serve
# só como filtro rápido antes de gastar uma requisição baixando o arquivo.
_BAD_IMAGE_URL_HINTS = (
    "logo", "icon", "favicon", "sprite", "avatar", "gravatar", "placeholder",
    "default", "blank", "1x1", "pixel", "spacer", "badge", "banner-ad",
)

# Meta tags conhecidas de imagem principal, em ordem de preferência —
# usadas como reforço/fallback ao que o trafilatura já extrai, pra casos
# em que o parser dele não encontra a tag (ordem de atributos inesperada,
# HTML malformado, etc.).
_IMAGE_META_PATTERNS = (
    re.compile(r'<meta[^>]+property=["\']og:image:secure_url["\'][^>]+content=["\']([^"\']+)', re.I),
    re.compile(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', re.I),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', re.I),
    re.compile(r'<meta[^>]+name=["\']twitter:image(?::src)?["\'][^>]+content=["\']([^"\']+)', re.I),
    re.compile(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+name=["\']twitter:image(?::src)?["\']', re.I),
    re.compile(r'<link[^>]+rel=["\']image_src["\'][^>]+href=["\']([^"\']+)', re.I),
)


def _looks_like_real_photo(url: str) -> bool:
    lowered = url.lower()
    if lowered.endswith(".svg"):
        return False
    return not any(hint in lowered for hint in _BAD_IMAGE_URL_HINTS)


def extract_source_image_candidates(html: str, page_url: str) -> list[str]:
    """Extrai candidatos a imagem principal da matéria-fonte, em ordem de
    confiança: primeiro o que o trafilatura lê (og:image/twitter:image via
    parser dedicado), depois um fallback via regex direto no HTML (cobre
    casos em que o parser não acha a tag). Filtra candidatos que parecem
    ser logo/ícone/avatar/pixel de rastreamento em vez de foto real —
    tentar a "foto certa" em vez da primeira imagem que aparecer."""
    if not html:
        return []

    candidates: list[str] = []

    try:
        meta = trafilatura.extract_metadata(html, default_url=page_url)
    except Exception as exc:  # noqa: BLE001
        print(f"    aviso: falha ao ler metadata da página-fonte ({exc})", file=sys.stderr)
        meta = None
    if meta:
        # extract_metadata() normalmente devolve um Document, mas tratamos
        # dict também pelo mesmo motivo do bare_extraction() acima: já
        # causou crash em produção assumir um formato só.
        image = meta.get("image") if isinstance(meta, dict) else getattr(meta, "image", None)
        if image:
            candidates.append(image)

    for pattern in _IMAGE_META_PATTERNS:
        match = pattern.search(html)
        if match:
            candidates.append(match.group(1))

    seen: set[str] = set()
    result: list[str] = []
    for raw in candidates:
        absolute = urljoin(page_url, raw)
        if absolute in seen:
            continue
        seen.add(absolute)
        if _looks_like_real_photo(absolute):
            result.append(absolute)
    return result


# Palavras que indicam que um texto próximo da imagem É um crédito de
# fotografia (e não só uma legenda descritiva qualquer) — usado só pra
# decidir se um <figcaption> vale como crédito.
_PHOTO_CREDIT_KEYWORDS = (
    "foto:", "fotos:", "crédito:", "credito:", "credit:", "credits:",
    "reprodução", "reproducao", "divulgação", "divulgacao",
    "reuters", "getty", "ap photo", "xpb", "lat images", "motorsport images",
)

# Padrões de crédito estruturado (schema.org ImageObject via JSON-LD),
# comum em CMS de portais de notícia profissionais — o jeito mais
# confiável de achar o crédito certo da foto, quando existe.
_STRUCTURED_CREDIT_PATTERNS = (
    re.compile(r'"creditText"\s*:\s*"([^"]{2,120})"', re.I),
    re.compile(r'"copyrightHolder"\s*:\s*\{[^}]*?"name"\s*:\s*"([^"]{2,120})"', re.I),
    re.compile(r'"creator"\s*:\s*\{[^}]*?"name"\s*:\s*"([^"]{2,120})"', re.I),
)

_FIGCAPTION_PATTERN = re.compile(r'<figcaption[^>]*>(.*?)</figcaption>', re.I | re.S)


def extract_photo_credit(html: str) -> str | None:
    """Tenta achar o crédito ESPECÍFICO da fotografia na página-fonte (quem
    tirou/detém a foto — ex.: "XPB Images", "Getty Images", um fotógrafo
    nomeado), pra usar quando a foto ORIGINAL é reaproveitada sem edição
    (ver get_ai_variation_image). Devolve None se não achar nada confiável
    — quem chama cai de volta pro nome do veículo nesse caso. Best-effort:
    olha primeiro dados estruturados (schema.org ImageObject via JSON-LD,
    o sinal mais confiável), depois legendas de figura (<figcaption>) que
    contenham uma palavra-chave de crédito."""
    if not html:
        return None

    for pattern in _STRUCTURED_CREDIT_PATTERNS:
        match = pattern.search(html)
        if match:
            credit = html_unescape(match.group(1)).strip()
            if credit:
                return credit[:160]

    for fig_match in _FIGCAPTION_PATTERN.finditer(html):
        text = re.sub(r"<[^>]+>", " ", fig_match.group(1))
        text = html_unescape(text).strip()
        text = re.sub(r"\s+", " ", text)
        if text and any(keyword in text.lower() for keyword in _PHOTO_CREDIT_KEYWORDS):
            return text[:160]

    return None


def download_image_bytes(
    url: str,
    max_bytes: int = 8_000_000,
    min_width: int = 480,
    min_height: int = 270,
) -> tuple[bytes, str] | None:
    """Baixa uma imagem e devolve (bytes, mime_type). None se falhar, vier
    vazia/pequena demais (provável placeholder), grande demais, ou com
    dimensões pequenas demais pra ser uma foto de matéria de verdade (em
    vez de um logo/ícone/thumbnail que passou pelo filtro de URL)."""
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
    if not content_type.startswith("image/") or content_type == "image/svg+xml":
        return None
    try:
        with Image.open(io.BytesIO(data)) as img:
            width, height = img.size
    except Exception:
        # Não conseguiu nem abrir como imagem — não é um arquivo confiável.
        return None
    if width < min_width or height < min_height:
        return None
    return data, content_type


def resize_for_generation(
    image_bytes: bytes,
    mime_type: str,
    scale: float = IMAGE_GEN_RESIZE_SCALE,
    max_dimension: int = IMAGE_GEN_MAX_DIMENSION,
) -> tuple[bytes, str]:
    """Reduz a imagem-fonte antes de mandar pra API do Gemini: corta
    `scale` do tamanho original (0.6 = fica com 60%, ou seja, reduz em
    40%) e, além disso, garante que a maior dimensão não passe de
    `max_dimension` px — o que for mais restritivo entre os dois vale.
    Se der qualquer problema pra reprocessar a imagem, devolve a original
    sem redimensionar (a chamada à API simplesmente sai mais cara nesse
    caso raro, em vez de quebrar a geração)."""
    try:
        with Image.open(io.BytesIO(image_bytes)) as img:
            width, height = img.size
            target_w = max(1, round(width * scale))
            target_h = max(1, round(height * scale))
            longest = max(target_w, target_h)
            if longest > max_dimension:
                factor = max_dimension / longest
                target_w = max(1, round(target_w * factor))
                target_h = max(1, round(target_h * factor))
            if (target_w, target_h) == (width, height):
                return image_bytes, mime_type
            resized = img.convert("RGB") if img.mode not in ("RGB", "RGBA") else img.copy()
            resized = resized.resize((target_w, target_h), Image.LANCZOS)
            buf = io.BytesIO()
            if mime_type == "image/png":
                resized.save(buf, format="PNG")
                out_mime = "image/png"
            else:
                resized.convert("RGB").save(buf, format="JPEG", quality=85)
                out_mime = "image/jpeg"
            return buf.getvalue(), out_mime
    except Exception as exc:  # noqa: BLE001
        print(f"    aviso: falha ao redimensionar imagem antes do envio ({exc}), usando original", file=sys.stderr)
        return image_bytes, mime_type


def generate_image_variation(
    image_bytes: bytes, mime_type: str
) -> tuple[bytes, str, dict | None] | None:
    """Chama a API do Gemini (Nano Banana) para gerar uma variação da
    imagem recebida. Devolve (bytes_da_imagem_gerada, mime_type,
    info_de_tokens) ou None. info_de_tokens é
    {"prompt": int, "resposta": int, "total": int} quando a API devolve
    usageMetadata, ou None se não vier."""
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
    usage = data.get("usageMetadata") or {}
    token_info = None
    if usage:
        token_info = {
            "prompt": usage.get("promptTokenCount", 0),
            "resposta": usage.get("candidatesTokenCount", 0),
            "total": usage.get("totalTokenCount", 0),
        }
    try:
        for candidate in data.get("candidates", []):
            for part in candidate.get("content", {}).get("parts", []):
                inline = part.get("inlineData") or part.get("inline_data")
                if inline and inline.get("data"):
                    out_mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
                    return base64.b64decode(inline["data"]), out_mime, token_info
    except Exception as exc:  # noqa: BLE001
        print(f"    aviso: resposta inesperada da API de imagem do Gemini ({exc})", file=sys.stderr)
    return None


def get_ai_variation_image(group_candidates: list[dict]) -> dict | None:
    """Tenta, na ordem das fontes do grupo, gerar uma variação por IA da
    imagem já usada na matéria-fonte. Devolve um dict
    {"url", "credit_name", "credit_url", "provider", "source_image_url",
    "tokens"} ou None se nenhuma fonte do grupo tiver sequer uma imagem
    baixável — aí a matéria fica sem imagem (o template usa o placeholder
    colorido por categoria). "source_image_url" é a URL da imagem ORIGINAL
    da matéria-fonte usada como base; "tokens" é
    {"prompt", "resposta", "total"} devolvido pela API do Gemini, ou None
    quando não há chamada de geração envolvida.

    Se a foto original foi baixada com sucesso mas a CHAMADA AO GEMINI
    falhar (rede, quota, resposta inválida etc.), a foto original da
    matéria-fonte é usada como está (sem variação por IA), com crédito —
    em vez de tentar outra fonte ou ficar sem imagem nenhuma. Pedido
    explícito do dono do projeto: preferir uma foto de verdade (creditada)
    a um placeholder genérico só porque a geração falhou numa rodada. O
    crédito, nesse caso, prioriza o crédito ESPECÍFICO da fotografia (se a
    página trouxer um — ver extract_photo_credit()) e só cai pro nome do
    veículo quando não acha nada mais específico. Quando a imagem É gerada
    por IA (variação), NUNCA leva crédito — não é uma foto de banco, é uma
    variação derivada da matéria-fonte."""
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
        image_candidates = extract_source_image_candidates(html, link)
        if not image_candidates:
            continue
        downloaded = None
        for image_url in image_candidates:
            downloaded = download_image_bytes(image_url)
            if downloaded:
                break
        if not downloaded:
            continue
        original_bytes, original_mime = downloaded
        resized_bytes, resized_mime = resize_for_generation(original_bytes, original_mime)
        variation = generate_image_variation(resized_bytes, resized_mime)
        digest = hashlib.sha1(link.encode("utf-8")).hexdigest()[:16]
        GENERATED_IMAGES_DIR.mkdir(parents=True, exist_ok=True)

        if variation:
            variation_bytes, out_mime, token_info = variation
            ext = "png" if "png" in out_mime else "jpg"
            filename = f"{digest}.{ext}"
            (GENERATED_IMAGES_DIR / filename).write_bytes(variation_bytes)
            return {
                "url": f"{GENERATED_IMAGES_URL_PREFIX}/{filename}",
                "credit_name": "",
                "credit_url": "",
                "provider": "IA (variação da imagem da fonte)",
                "source_image_url": image_url,
                "tokens": token_info,
            }

        # Geração por IA falhou, mas já temos a foto original baixada e
        # validada (passou pelo filtro de tamanho/dimensões em
        # download_image_bytes) — usa ela direto, com crédito. Prioridade
        # do crédito: (1) o crédito ESPECÍFICO da fotografia, se a página
        # trouxer um (schema.org/figcaption — ex.: "XPB Images", um
        # fotógrafo nomeado); (2) se não achar nada específico, cai pro
        # nome do veículo, que ainda é uma atribuição válida (é de lá que
        # a foto veio).
        photo_credit = extract_photo_credit(html) or c.get("name", "")
        print(
            f"    aviso: geração por IA falhou pra essa fonte, usando a foto "
            f"original ({photo_credit or link}) (com crédito)",
            file=sys.stderr,
        )
        ext = "png" if "png" in original_mime else "jpg"
        filename = f"{digest}-original.{ext}"
        (GENERATED_IMAGES_DIR / filename).write_bytes(original_bytes)
        return {
            "url": f"{GENERATED_IMAGES_URL_PREFIX}/{filename}",
            "credit_name": photo_credit,
            "credit_url": link,
            "provider": "Foto original da matéria-fonte",
            "source_image_url": image_url,
            "tokens": None,
        }
    return None


# --------------------------------------------------------------------------
# Gravação do Markdown
# --------------------------------------------------------------------------

def toml_list(items) -> str:
    return "[" + ", ".join(f'"{str(i).replace(chr(34), "")}"' for i in items) + "]"


def post_filename_base(article: dict, date: datetime) -> str:
    """Nome de arquivo (sem extensão) usado tanto pro post quanto pra sua
    página de cards — precisam ser IGUAIS pra que os permalinks de
    site/content/posts e site/content/cards caiam no mesmo :year/:month/
    :slug (ver [permalinks] em site/hugo.toml) e a página de cards fique
    em <permalink-do-post>cards/."""
    slug = slugify(article["titulo"])[:70] or "materia"
    return f"{date:%Y-%m-%d}-{slug}"


def write_post(
    article: dict,
    source_names: list[str],
    source_urls: list[str],
    date: datetime,
    image_info: dict | None = None,
    has_cards: bool = False,
    author_name: str = AUTHOR_NAME,
) -> Path:
    filename_base = post_filename_base(article, date)
    path = POSTS_DIR / f"{filename_base}.md"
    image_info = image_info or {}

    def esc(s: str) -> str:
        return str(s).replace('"', "'")

    frontmatter = "\n".join(
        [
            "+++",
            f'title = "{esc(article["titulo"])}"',
            f"date = {date:%Y-%m-%dT%H:%M:%SZ}",
            f'author = "{esc(author_name)}"',
            f'summary = "{esc(article.get("linha_fina", ""))}"',
            f'categories = {toml_list([article.get("categoria", ALLOWED_CATEGORIES[0])])}',
            f'tags = {toml_list(article.get("tags", []))}',
            f"sources = {toml_list(source_names)}",
            f"source_urls = {toml_list(source_urls)}",
            f'image = "{esc(image_info.get("url", ""))}"',
            f'image_credit_name = "{esc(image_info.get("credit_name", ""))}"',
            f'image_credit_url = "{esc(image_info.get("credit_url", ""))}"',
            f'image_provider = "{esc(image_info.get("provider", ""))}"',
            f"has_cards = {str(has_cards).lower()}",
            "+++",
            "",
        ]
    )
    body = article.get("corpo_markdown", "").strip()
    path.write_text(frontmatter + body + "\n", encoding="utf-8")
    return path


def write_cards_page(filename_base: str, article: dict, date: datetime, image_urls: list[str]) -> Path:
    """Grava site/content/cards/<mesmo-nome-do-post>.md — uma página
    "irmã" do post, só com a lista de imagens dos cards. O permalink
    dessa seção (ver site/hugo.toml) é o mesmo do post + 'cards/'."""
    CARDS_CONTENT_DIR.mkdir(parents=True, exist_ok=True)
    path = CARDS_CONTENT_DIR / f"{filename_base}.md"

    def esc(s: str) -> str:
        return str(s).replace('"', "'")

    frontmatter = "\n".join(
        [
            "+++",
            f'title = "{esc(article["titulo"])}"',
            f"date = {date:%Y-%m-%dT%H:%M:%SZ}",
            f"images = {toml_list(image_urls)}",
            "+++",
            "",
        ]
    )
    path.write_text(frontmatter, encoding="utf-8")
    return path


def generate_and_render_cards(
    article: dict,
    image_info: dict | None,
    filename_base: str,
    date: datetime,
    writer: WriterProfile,
) -> list[str]:
    """Gera os textos dos cards (LLM, na voz do MESMO `writer` que escreveu
    a matéria) e renderiza as imagens (Pillow), gravando a página irmã em
    site/content/cards/. Devolve a lista de URLs das imagens geradas (vazia
    se algo falhar — ver call sites: isso NUNCA deve impedir a matéria em
    si de ser publicada)."""
    texts = generate_card_texts(article, writer)

    background_path = None
    if image_info and image_info.get("url", "").startswith(GENERATED_IMAGES_URL_PREFIX):
        candidate = ROOT / "site" / "static" / image_info["url"].lstrip("/")
        if candidate.exists():
            background_path = candidate

    out_dir = GENERATED_CARDS_DIR / filename_base
    category = article.get("categoria", ALLOWED_CATEGORIES[0])
    saved_paths = cards_module.generate_card_images(texts, category, background_path, out_dir)
    image_urls = [f"{GENERATED_CARDS_URL_PREFIX}/{filename_base}/{p.name}" for p in saved_paths]
    write_cards_page(filename_base, article, date, image_urls)
    return image_urls


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def _new_run_stats() -> dict:
    """Contadores mutáveis de uma rodada (funil automático OU modo manual),
    preenchidos por process_group(). Compartilhado entre todas as chamadas
    de process_group() numa mesma rodada, pra acumular os totais que saem
    no resumo verboso no fim (ver main()/run_manual_mode())."""
    return {
        "n_sem_texto_fonte": 0,
        "n_enviados_escrita": 0,
        "n_descartados_pos_escrita": 0,
        "n_enviados_factcheck": 0,
        "n_factcheck_falhou": 0,
        "n_descartados_pos_factcheck": 0,
        "n_enviados_imagem": 0,
        "n_imagem_gerada": 0,
        "n_imagem_ausente": 0,
        "n_imagem_tokens_total": 0,
        "n_enviados_cards": 0,
        "n_cards_gerados": 0,
        "n_cards_falhou": 0,
        "writer_usage": {w.key: 0 for w in WRITER_PROFILES},
        "published_titles": [],
        "created": 0,
    }


def process_group(group_candidates: list[dict], titles_preview: str, stats: dict) -> None:
    """Processa um grupo de candidatos (uma ou mais fontes do MESMO fato)
    até publicar (ou descartar) a matéria: escreve, revisa fatos, gera
    imagem, gera cards e grava o post. Mutação em `stats` (ver
    _new_run_stats()) igual ao que o loop de main() fazia inline antes
    dessa função existir — extraído pra ser reaproveitado tanto pelo funil
    automático (rodada normal, um grupo por vez dentro do `for group in
    groups`) quanto pelo modo manual (run_manual_mode(), um único grupo
    feito a partir de link(s) informados à mão via MANUAL_URLS).

    Não marca links como vistos — isso é responsabilidade de quem chama
    (no finally do laço, ou em run_manual_mode), porque só quem chama sabe
    se está lidando com `seen.json` do funil automático ou não."""
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

        print(f"    textos-fonte utilizáveis: {len(source_blocks)}/{min(len(group_candidates), 4)}")

        if not source_blocks:
            stats["n_sem_texto_fonte"] += 1
            print("    pulei: nenhum texto-fonte utilizável")
            return

        writer = select_writer(len(source_blocks))
        stats["writer_usage"][writer.key] += 1
        stats["n_enviados_escrita"] += 1
        print(
            f"    escritor escolhido: {writer.author_name} "
            f"({len(source_blocks)} fonte(s) utilizável(is), mínimo do escritor: {writer.min_sources})"
        )
        print(f"    escrevendo matéria ({active_text_model(MODEL)})...")
        article = rewrite_with_claude(source_blocks, writer)

        if is_insufficient_content(article):
            # O editor (rewrite_with_claude) sinalizou que os
            # textos-fonte não davam pra uma matéria de verdade. Não
            # publica, e — importante — não chama factcheck nem gera
            # imagem por IA: essa checagem tem que vir ANTES de
            # qualquer chamada cara, senão fica gastando API à toa com
            # algo que nunca vai virar post.
            stats["n_descartados_pos_escrita"] += 1
            print(f"    descartado (conteúdo insuficiente): {titles_preview[:70]}")
            return

        stats["n_enviados_factcheck"] += 1
        print(f"    revisando fatos ({active_text_model(FACTCHECK_MODEL)})...")
        try:
            article = factcheck_with_claude(article, source_blocks, writer)
        except Exception as exc:  # noqa: BLE001
            stats["n_factcheck_falhou"] += 1
            print(f"    aviso: revisão de fatos falhou, publicando rascunho ({exc})", file=sys.stderr)

        if is_insufficient_content(article):
            # A revisão de fatos (a ÚLTIMA etapa de verificação antes de
            # publicar) pode ter removido tanta coisa não-verificável do
            # rascunho que não sobrou matéria de verdade. Mesma regra:
            # descarta ANTES de gerar imagem — nunca gera imagem por IA
            # para uma matéria que não vai ser publicada.
            stats["n_descartados_pos_factcheck"] += 1
            print(f"    descartado (conteúdo insuficiente após revisão de fatos): {titles_preview[:70]}")
            return

        source_names = [c["name"] for c in group_candidates]
        links = [c["link"] for c in group_candidates]
        publish_date = max(c["date"] for c in group_candidates)

        stats["n_enviados_imagem"] += 1
        print(f"    gerando imagem ({GEMINI_IMAGE_MODEL})...")
        image_info = get_ai_variation_image(group_candidates)
        if image_info:
            stats["n_imagem_gerada"] += 1
            tokens = image_info.get("tokens")
            if tokens:
                stats["n_imagem_tokens_total"] += tokens.get("total", 0)
                tokens_str = (
                    f"prompt={tokens.get('prompt', 0)} "
                    f"resposta={tokens.get('resposta', 0)} "
                    f"total={tokens.get('total', 0)}"
                )
            else:
                tokens_str = "não informado pela API"
            print(f"    imagem: ok ({image_info.get('provider', '?')})")
            print(f"      original usada: {image_info.get('source_image_url', '?')}")
            print(f"      tokens (Gemini): {tokens_str}")
        else:
            stats["n_imagem_ausente"] += 1
            print("    imagem: nenhuma (sem GEMINI_API_KEY, sem imagem na fonte, ou falha na API)")

        filename_base = post_filename_base(article, publish_date)

        stats["n_enviados_cards"] += 1
        print(f"    gerando cards de Stories ({active_text_model(CARDS_MODEL)})...")
        card_urls: list[str] = []
        try:
            card_urls = generate_and_render_cards(
                article, image_info, filename_base, publish_date, writer
            )
        except Exception as exc:  # noqa: BLE001
            stats["n_cards_falhou"] += 1
            print(f"    aviso: geração de cards falhou, publicando matéria sem cards ({exc})", file=sys.stderr)
        if card_urls:
            stats["n_cards_gerados"] += 1
            print(f"    cards: {len(card_urls)} gerado(s)")

        write_post(
            article,
            source_names,
            links,
            publish_date,
            image_info,
            has_cards=bool(card_urls),
            author_name=writer.author_name,
        )
        stats["created"] += 1
        stats["published_titles"].append(article.get('titulo', ''))
        print(f"    ✓ publicado: {article.get('titulo', '')[:70]}")
    except Exception as exc:  # noqa: BLE001
        print(f"    erro ao gerar/gravar: {exc}", file=sys.stderr)


def build_manual_candidate(url: str) -> dict | None:
    """Baixa e extrai o texto de UM link informado manualmente (modo
    manual, ver run_manual_mode()/MANUAL_URLS), no mesmo formato de
    candidato usado pelo funil automático. Reaproveita a mesma lógica de
    extração de collect_list_candidates (trafilatura.bare_extraction,
    tratando os dois formatos de retorno — dict ou objeto Document), mas
    pra um único link já escolhido manualmente: não passa pela página de
    listagem, nem pelo filtro de idade da notícia, nem por `seen.json`
    (o link foi escolhido de propósito, mesmo que já tenha aparecido
    antes numa rodada automática)."""
    try:
        article_html = trafilatura.fetch_url(url)
    except Exception as exc:  # noqa: BLE001
        print(f"    erro ao baixar {url} ({exc})", file=sys.stderr)
        return None
    if not article_html:
        print(f"    não consegui baixar {url}", file=sys.stderr)
        return None
    try:
        data = trafilatura.bare_extraction(article_html, with_metadata=True)
    except Exception as exc:  # noqa: BLE001
        print(f"    erro ao extrair texto de {url} ({exc})", file=sys.stderr)
        return None
    if not data:
        print(f"    não consegui extrair texto de {url}", file=sys.stderr)
        return None
    # trafilatura pode devolver um dict OU um objeto Document, dependendo
    # da versão/parâmetros — mesmo tratamento de collect_list_candidates.
    if isinstance(data, dict):
        text = data.get("text")
        title = data.get("title")
        raw_date = data.get("date")
        sitename = data.get("sitename")
    else:
        text = getattr(data, "text", None)
        title = getattr(data, "title", None)
        raw_date = getattr(data, "date", None)
        sitename = getattr(data, "sitename", None)
    if not text or len(text) < 120:
        print(f"    texto extraído de {url} é curto demais pra virar matéria", file=sys.stderr)
        return None
    title = title or url
    name = sitename or urlparse(url).netloc.replace("www.", "")
    date = datetime.now(timezone.utc)
    if raw_date:
        try:
            date = datetime.fromisoformat(str(raw_date)).replace(tzinfo=timezone.utc)
        except Exception:  # noqa: BLE001
            pass
    return {
        "name": name,
        "extra_instructions": None,
        "title": title,
        "link": url,
        "summary": text[:500],
        "date": date,
        "_full_text": text,
        "_source_html": article_html,
    }


def run_manual_mode(urls: list[str]) -> int:
    """Modo manual (env MANUAL_URLS): recebe um ou mais links de
    matéria-fonte já escolhidos por uma pessoa, e produz UMA matéria a
    partir deles — mesmo funil de escrita/revisão/imagem/cards de sempre
    (process_group), só que pulando coleta (sources.yaml) e agrupamento/
    classificação por LLM (cluster_and_classify), já que não há o que
    agrupar ou classificar por escopo: a pessoa já decidiu que esse
    conteúdo deve virar matéria. Os links processados são marcados em
    `seen.json` no final, pra o funil automático não tentar publicar a
    mesma notícia de novo depois."""
    print(f"Modo manual: {len(urls)} link(s) informado(s) via MANUAL_URLS")
    seen = load_seen()

    group_candidates: list[dict] = []
    for url in urls:
        print(f"\n  baixando e extraindo: {url}")
        candidate = build_manual_candidate(url)
        if candidate is None:
            print("    pulei (falha ao baixar/extrair texto utilizável)", file=sys.stderr)
            continue
        print(f"    ok: fonte=\"{candidate['name']}\" título-fonte=\"{candidate['title'][:70]}\"")
        group_candidates.append(candidate)

    if not group_candidates:
        print("\nNenhum dos links informados pôde ser processado. Concluído (nada publicado).", file=sys.stderr)
        return 1

    links = [c["link"] for c in group_candidates]
    titles_preview = " | ".join(c["title"][:50] for c in group_candidates)
    print(f"\n  + [manual] {titles_preview}")
    print(f"    fontes no grupo: {len(group_candidates)}")

    stats = _new_run_stats()
    try:
        process_group(group_candidates, titles_preview, stats)
    finally:
        for link in links:
            seen.add(link)
        save_seen(seen)

    print("\n" + "=" * 60)
    if stats["created"] > 0:
        print(f"✓ Matéria publicada: {stats['published_titles'][0]}")
        print("=" * 60)
        return 0

    print("Não foi possível publicar uma matéria a partir do(s) link(s) informado(s).", file=sys.stderr)
    print("=" * 60)
    return 1


def main() -> int:
    POSTS_DIR.mkdir(parents=True, exist_ok=True)

    manual_urls_raw = os.environ.get("MANUAL_URLS", "").strip()
    if manual_urls_raw:
        urls = [u.strip() for u in re.split(r"[\n,]+", manual_urls_raw) if u.strip()]
        return run_manual_mode(urls)

    config = yaml.safe_load(SOURCES_FILE.read_text(encoding="utf-8"))
    feeds = config.get("feeds", [])
    seen = load_seen()
    seen_antes = len(seen)

    print(f"seen.json: {seen_antes} link(s) já conhecido(s) (rodadas anteriores)")
    if DEEPSEEK_API_KEY:
        print(f"Modelo de texto EM USO: DeepSeek ({DEEPSEEK_MODEL}) — Claude não é "
              f"chamado (CLUSTER_MODEL/MODEL/FACTCHECK_MODEL configurados mas "
              f"ignorados: {CLUSTER_MODEL}/{MODEL}/{FACTCHECK_MODEL}; apague "
              f"DEEPSEEK_API_KEY pra usá-los de novo). GEMINI_IMAGE_MODEL={GEMINI_IMAGE_MODEL}"
              f"{' (sem GEMINI_API_KEY, imagem sempre pulada)' if not GEMINI_API_KEY else ''}")
    else:
        print(f"Modelos: CLUSTER_MODEL={CLUSTER_MODEL} | MODEL={MODEL} | "
              f"FACTCHECK_MODEL={FACTCHECK_MODEL} | GEMINI_IMAGE_MODEL={GEMINI_IMAGE_MODEL}"
              f"{' (sem GEMINI_API_KEY, imagem sempre pulada)' if not GEMINI_API_KEY else ''}")

    candidates = collect_all_candidates(feeds, seen)
    print(f"\nTotal de manchetes novas coletadas (2ª fase): {len(candidates)}")

    if not candidates:
        save_seen(seen)
        print("Nada novo. Concluído.")
        return 0

    now = datetime.now(timezone.utc)
    recent_context = load_recent_published_context(now)
    print(
        f"Contexto: {len(recent_context)} matéria(s) publicada(s) nas últimas "
        f"{RECENT_CONTEXT_WINDOW_HOURS}h consideradas (evita publicar notícia "
        f"redundante/já superada, ex.: classificação depois do resultado da corrida)"
    )

    print(f"\nAgrupando e classificando manchetes ({active_text_model(CLUSTER_MODEL)})...")
    try:
        groups = cluster_and_classify(candidates, recent_context)
    except Exception as exc:  # noqa: BLE001
        print(f"erro ao agrupar/classificar: {exc}", file=sys.stderr)
        # NÃO marca como visto: falha no agrupamento é normalmente transitória
        # (rede, resposta inesperada). Assim as mesmas manchetes são
        # tentadas de novo na próxima rodada em vez de se perderem.
        return 1

    n_groups = len(groups)
    n_sem_ids = 0
    n_fora_de_escopo = 0
    grouped_ids: set[int] = set()
    stats = _new_run_stats()

    print(f"\nAgregação: {n_groups} grupo(s) formado(s) a partir de {len(candidates)} manchete(s)")

    for group in groups:
        ids = [i for i in group.get("ids", []) if 0 <= i < len(candidates)]
        categoria = group.get("categoria", "DESCARTAR")
        grouped_ids.update(ids)
        if not ids:
            n_sem_ids += 1
            continue

        group_candidates = [candidates[i] for i in ids]
        links = [c["link"] for c in group_candidates]

        if categoria not in ALLOWED_CATEGORIES:
            n_fora_de_escopo += 1
            print(f"  descartado (fora do escopo): {group_candidates[0]['title'][:70]}")
            for link in links:
                seen.add(link)
            continue

        titles_preview = " | ".join(c["title"][:50] for c in group_candidates)
        print(f"\n  + [{categoria}] {titles_preview}")
        print(f"    fontes no grupo: {len(group_candidates)}")

        try:
            process_group(group_candidates, titles_preview, stats)
        finally:
            for link in links:
                seen.add(link)

    for i, c in enumerate(candidates):
        if i not in grouped_ids:
            seen.add(c["link"])

    save_seen(seen)

    seen_depois = len(seen)
    print("\n" + "=" * 60)
    print("Resumo da rodada")
    print("=" * 60)
    print(f"Manchetes coletadas (2ª fase):        {len(candidates)}")
    print(f"Grupos formados na agregação:          {n_groups}"
          f" (sem ids: {n_sem_ids}, fora do escopo: {n_fora_de_escopo})")
    print(f"Grupos sem texto-fonte (pulados):      {stats['n_sem_texto_fonte']}")
    print(f"Enviados para escrita ({active_text_model(MODEL)}):  {stats['n_enviados_escrita']}")
    print(f"  descartados após escrita (insuficiente):     {stats['n_descartados_pos_escrita']}")
    print(f"Enviados para revisão de fatos ({active_text_model(FACTCHECK_MODEL)}): {stats['n_enviados_factcheck']}")
    print(f"  falhas na chamada (publicado sem revisão):   {stats['n_factcheck_falhou']}")
    print(f"  descartados após revisão (insuficiente):     {stats['n_descartados_pos_factcheck']}")
    print(f"Enviados para gerar imagem ({GEMINI_IMAGE_MODEL}): {stats['n_enviados_imagem']}")
    print(f"  imagem gerada com sucesso:                   {stats['n_imagem_gerada']}")
    print(f"  sem imagem (falha/sem chave/sem foto-fonte): {stats['n_imagem_ausente']}")
    print(f"  tokens (Gemini) usados na geração de imagem: {stats['n_imagem_tokens_total']}")
    print(f"Enviados para gerar cards ({active_text_model(CARDS_MODEL)}): {stats['n_enviados_cards']}")
    print(f"  cards gerados com sucesso:                   {stats['n_cards_gerados']}")
    print(f"  falha na geração (publicado sem cards):      {stats['n_cards_falhou']}")
    print("Escritor escolhido por matéria:")
    for w in WRITER_PROFILES:
        print(f"  {w.author_name} (min. {w.min_sources} fonte(s)): {stats['writer_usage'][w.key]}")
    print(f"Matérias publicadas:                   {stats['created']}")
    if stats["published_titles"]:
        print("Títulos publicados nesta rodada:")
        for titulo in stats["published_titles"]:
            print(f"  - {titulo}")
    if DEEPSEEK_API_KEY:
        print(f"Chamadas de texto respondidas pelo DeepSeek: {LLM_CALL_STATS['deepseek_ok']}")
    print(f"seen.json: {seen_antes} antigo(s) + {seen_depois - seen_antes} novo(s)"
          f" marcado(s) nesta rodada = {seen_depois} no total")
    print("=" * 60)
    print(f"\nConcluído: {stats['created']} matéria(s) nova(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
