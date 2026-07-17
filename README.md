# BRGrid — portal de notícias de automobilismo (automatizado)

Pipeline que lê notícias de várias fontes (RSS e sites sem feed), agrupa
manchetes que tratam do mesmo fato vindas de fontes diferentes, reescreve o
resultado em português com a **API do Claude**, revisa os fatos com um
segundo passe usando um **modelo mais forte (Sonnet)** e publica um site
estático (**Hugo**) que se atualiza sozinho. Sem servidor para manter.

**Escopo:** Kart, F1, F2, F3, F4, GT3, WEC, IndyCar e NASCAR.

```
fontes (RSS + sites sem feed) → agrupar/classificar → Haiku reescreve
        → Sonnet revisa fatos → Markdown → Hugo → site publicado
                    (GitHub Actions, a cada 3h)         (Cloudflare Pages)
```

## Para modelos de IA / agentes que forem mexer neste repositório

Leia esta seção inteira antes de editar qualquer coisa — ela existe pra você
não perder tempo redescobrindo decisões e bugs que já foram resolvidos.

**O que este projeto é, em uma frase:** um script Python (`pipeline/generate.py`)
que roda no GitHub Actions a cada 3h, lê notícias de várias fontes, usa a API
do Claude em três etapas (agrupar → escrever → revisar), grava o resultado
como arquivos Markdown, e um site Hugo estático é buildado e publicado no
Cloudflare Pages — tudo dentro do mesmo workflow, sem servidor nenhum rodando
o tempo todo.

### Mapa mental do fluxo (`pipeline/generate.py`, função `main()`)
1. `collect_all_candidates()` — para cada fonte em `sources.yaml`, coleta
   manchetes novas (não presentes em `seen.json`). Duas variantes:
   `collect_rss_candidates` (feed normal) e `collect_list_candidates`
   (site sem RSS: baixa a página de listagem, extrai links por regex +
   `link_contains`, baixa cada artigo com `trafilatura`).
2. `cluster_and_classify()` — UMA chamada ao Claude (Sonnet) recebe TODAS as
   manchetes da rodada e devolve grupos: quais manchetes tratam do mesmo
   fato (mesmo vindas de fontes diferentes) e qual categoria cada grupo tem
   — ou `"DESCARTAR"` se estiver fora do escopo do site.
3. Para cada grupo válido: baixa o texto completo de cada fonte do grupo,
   chama `rewrite_with_claude()` (Haiku, gera UMA matéria agregando as
   fontes) e depois `factcheck_with_claude()` (Sonnet, compara o rascunho
   contra os textos-fonte e corrige/remove o que não está lá).
4. `write_post()` grava o Markdown em `site/content/posts/` com frontmatter
   TOML. Note que `sources` e `source_urls` são LISTAS (uma matéria pode ter
   várias fontes) — não existe mais `source_name`/`source_url` singular.

### Decisões não-óbvias (e por quê)
- **Categorias são uma lista fechada** (`ALLOWED_CATEGORIES`). Qualquer
  manchete fora disso é descartada na etapa de classificação — o filtro
  acontece ANTES de baixar o texto completo ou gastar tokens de geração,
  pra economizar. Se for ampliar o escopo, mexa em `ALLOWED_CATEGORIES` E
  nos partials Hugo `catkey.html`/`catlabel.html`/CSS (cor por categoria) —
  os três precisam ficar em sincronia manualmente, não há fonte única.
- **Sem link para a matéria original no rodapé.** Decisão explícita do
  dono do projeto — a fonte é creditada só pelo nome, no topo. Não
  reintroduza o link sem confirmar com ele.
- **`seen.json` é a memória do pipeline.** Ele impede reprocessar a mesma
  URL. Se você resetar categorias/fontes de forma incompatível com o
  conteúdo já publicado (como aconteceu na migração pra "BRGrid"), o mais
  simples é apagar todos os `.md` de `site/content/posts/` E zerar
  `seen.json` para `[]` juntos, senão sobra lixo misturado.
- **Falha no agrupamento NÃO marca manchetes como vistas** (de propósito —
  já causei perda de ~29 manchetes reais fazendo isso errado numa versão
  anterior). Se mexer no tratamento de erro de `cluster_and_classify`,
  preserve esse comportamento.
- **Antes de adicionar uma fonte `type: list` (sem RSS), confira o
  `robots.txt` do domínio e avise o dono do projeto se ele proibir acesso
  automatizado.** Isso não bloqueia a fonte automaticamente — é uma decisão
  do dono do projeto, ciente do risco (bloqueio de IP, zona cinzenta
  jurídica). Caso real: `kartmotor.com.br` tem robots.txt proibindo, o
  dono decidiu tentar mesmo assim, mas na prática o site bloqueia a
  requisição em nível de servidor de qualquer forma — então acabou não
  importando quem "decidiu" o quê; a fonte foi removida por simplesmente
  não funcionar. Ver seção de fontes com histórico de problema, mais abaixo.
- **`trafilatura.bare_extraction()` às vezes devolve um objeto `Document`,
  às vezes um `dict`**, dependendo da versão/parâmetros — já causou um
  crash em produção. `collect_list_candidates` trata os dois casos
  explicitamente; não assuma um formato só se tocar nesse código.
- **Formatação de data do Hugo:** o layout do Go só reconhece o token
  `"Jan"` (maiúsculo) para nome de mês — `"jan"` minúsculo não é um token
  válido e o Hugo imprime a string literal (bug real que já aconteceu
  aqui: toda data aparecia "jan" fixo). As datas em português usam os
  partials `ptmonth.html`/`ptmonth_long.html`, não `.Date.Format` direto
  com nomes de mês.
- **O projeto no Cloudflare Pages precisa existir ANTES do primeiro
  `wrangler pages deploy`** — em ambiente não-interativo (CI) o wrangler
  não cria o projeto sozinho. O workflow tem um passo `curl` idempotente
  que garante isso a cada rodada (ignora erro de "já existe" com `|| true`).

### Se algo quebrar e você precisar depurar um workflow run
Ambientes de agente costumam ter acesso de rede restrito e não conseguem
baixar os logs brutos do GitHub Actions (o endpoint de logs redireciona
para `results-receiver.actions.githubusercontent.com` ou domínios de blob
storage, tipicamente fora de qualquer allowlist). Truque que funcionou aqui:
adicione um passo temporário no workflow que redireciona a saída do script
para um arquivo e faz commit dele no repo (`git add/commit/push` dentro do
próprio job), aí dá pra ler o conteúdo via API do GitHub normalmente
(`GET /repos/.../contents/{path}`). Remova esse passo depois de resolver —
não é pra ficar em produção.

Outro detalhe: um step com `continue-on-error: true` que falhou aparece
como `conclusion: "success"` na API de jobs (o campo que reflete a falha
real é `outcome`, que a API de listagem de jobs não expõe). Não confie só
no `conclusion` pra saber se a geração realmente funcionou — confira o log.

### Fontes que já tiveram problema (histórico)
`Vroomkart` estava configurado com uma URL de RSS que não funciona mais
(`vroomkart.com/rss.xml`) — o site não expõe RSS ativo, então foi trocado
para raspagem de listagem (`type: list`, mesma técnica dos sites
brasileiros), apontando para `vroomkart.com/news`.

`Kart Motor` (kartmotor.com.br) foi tentado e removido. Histórico: o
robots.txt do site proíbe acesso automatizado; o dono do projeto decidiu
inicialmente manter a fonte mesmo assim (e a feature de instrução extra
por fonte, abaixo, nasceu justamente para filtrar o conteúdo dela). Mas na
prática o `fetch_url` nunca conseguiu baixar a página de listagem a partir
do runner do GitHub Actions (nenhuma URL de kartmotor.com.br apareceu em
`seen.json` mesmo após rodadas reais) — o site provavelmente bloqueia a
requisição em nível de servidor (User-Agent, WAF, etc.), não só via
robots.txt. Não vale reintroduzir sem antes resolver esse bloqueio (ex.:
testar um User-Agent de navegador); do jeito que estava, era uma fonte
morta que não trazia nada.

### Prompt extra por fonte (`extra_instructions`)
Qualquer fonte em `sources.yaml` pode ter um campo opcional
`extra_instructions` com uma instrução em texto livre. Ela é injetada em
dois pontos do pipeline:
- Na etapa de agrupamento/classificação (`cluster_and_classify`), anexada
  à linha daquela manchete como `regra da fonte: ...` — o modelo é
  instruído a simplesmente OMITIR da lista de grupos qualquer manchete
  que viole a regra da fonte dela, mesmo que o assunto geral esteja no
  escopo do site. Essa é a defesa principal.
- Na escrita e na revisão de fatos (`rewrite_with_claude` /
  `factcheck_with_claude`), como uma nota `[Instrução especial para esta
  fonte: ...]` junto do bloco de texto daquela fonte — defesa secundária,
  caso algo passe pelo filtro de agrupamento (ex.: numa matéria agregada
  com outras fontes que não têm essa regra).

Exemplo (histórico, não está mais em uso já que a fonte foi removida):
`Kart Motor` tinha `extra_instructions: "Nas notícias de kart, usar apenas
notícias referentes a eventos e resultados. Não utilizar notícias
referentes a pilotos específicos..."` — filtrava matérias focadas num
piloto específico vindas daquela fonte. O mecanismo continua disponível e
testado (ver `pipeline/generate.py`, funções `cluster_and_classify`,
`rewrite_with_claude`, `factcheck_with_claude`) para qualquer fonte futura
que precisar de uma regra própria.

### Imagens: banco de fotos, sem ilustração de reserva
Cada matéria tenta uma foto de banco (Pexels) por um termo genérico da
categoria (`CATEGORY_STOCK_QUERIES` em `pipeline/generate.py`; não dá pra
achar foto do evento específico da matéria, então a busca é por categoria
mesmo) e escolhe uma foto aleatória entre os resultados. Precisa do secret
`PEXELS_API_KEY` (gratuito, sem cartão — pexels.com/api).

Se não achar (ou não tiver a chave configurada), a matéria fica **sem
imagem** — `image` vazio — e o template usa o placeholder colorido por
categoria que já existia (listra diagonal + nome da categoria). Não
tentamos mais gerar uma ilustração por IA como reserva: já foi testado
(via Pollinations.ai) e o resultado visual não ficou bom o suficiente,
então foi removido de propósito. Se quiser retomar essa ideia depois, dá
pra ver como estava implementado no histórico do git (commit que adiciona
"banco de fotos Pexels + ilustração de categoria"), mas não reintroduza
sem confirmar com o dono do projeto — foi uma decisão consciente de tirar.

Licença: fotos do Pexels não exigem atribuição (mas é bem-vindo creditar,
se algum dia quiser adicionar isso).

## Estrutura

```
pipeline/
  sources.yaml        fontes: RSS ou sites sem feed (raspagem de listagem)
  generate.py          coleta, agrupa, gera e revisa as matérias
  requirements.txt    dependências Python
  seen.json           controle de matérias já publicadas (não apague)
.github/workflows/
  update.yml          agenda a cada 3h + botão "Run workflow"
site/
  hugo.toml           config do site (troque baseURL após publicar)
  layouts/            templates (home tipo portal, cards com cor por categoria)
  static/css/         estilo
  content/posts/      matérias geradas (começa vazio)
```

## Como colocar no ar (passo a passo)

### 1. Suba o projeto no GitHub
Crie um repositório novo e envie estes arquivos.

### 2. Guarde a chave da API do Claude
No repositório: **Settings → Secrets and variables → Actions → New repository secret**
- Nome: `ANTHROPIC_API_KEY`
- Valor: sua chave (crie em <https://console.anthropic.com/>)

### 2b. (Opcional) Chave do Pexels para fotos de banco
Mesmo caminho de secret, nome `PEXELS_API_KEY` — crie em
<https://www.pexels.com/api/> (gratuito, instantâneo). Sem essa chave, as
matérias ficam sem foto (usa o placeholder colorido por categoria) — não
quebra nada.

### 3. Hospedagem: Cloudflare Pages (já automatizado)
O próprio workflow do GitHub Actions builda o site com Hugo e publica no
Cloudflare Pages a cada rodada — não depende de conectar o repositório pela
interface do Cloudflare. Para isso, dois secrets adicionais no repositório:
- `CLOUDFLARE_API_TOKEN` — token customizado com permissão de conta
  **Cloudflare Pages: Edit** (crie em dash.cloudflare.com/profile/api-tokens →
  Create Custom Token)
- `CLOUDFLARE_ACCOUNT_ID` — ID da conta (aparece na URL do dashboard)

Este projeto já está publicado em: **https://portal-noticias-cz7.pages.dev/**
(o Cloudflare pode adicionar um sufixo aleatório ao nome do projeto se houver
colisão — confira o `subdomain` retornado na criação do projeto, ou o próprio
dashboard, e ajuste `baseURL` em `site/hugo.toml` se for diferente).

### 4. Gere as primeiras matérias
No GitHub: aba **Actions → "Atualizar notícias" → Run workflow**.
O script roda, grava as matérias, faz commit — e o Cloudflare publica.
Depois disso, roda sozinho a cada 3 horas.

## Rodar localmente (opcional)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r pipeline/requirements.txt

export ANTHROPIC_API_KEY="sua-chave"
python pipeline/generate.py           # gera matérias reais
# ou, sem gastar API (texto de teste, mas ainda lê os feeds):
DRY_RUN=1 python pipeline/generate.py

# pré-visualizar o site:
cd site && hugo server
```

## Ajustes rápidos

| O que | Onde |
|---|---|
| Trocar/adicionar fontes | `pipeline/sources.yaml` (RSS ou `type: list` p/ sites sem feed) |
| Categorias cobertas | `ALLOWED_CATEGORIES` em `pipeline/generate.py` |
| Manchetes por fonte a cada rodada | `MAX_PER_FEED` (workflow ou env), padrão 4 |
| Frequência de atualização | linha `cron` em `.github/workflows/update.yml` |
| Modelo de geração (1ª passada) | env `MODEL` (padrão: `claude-haiku-4-5-20251001`) |
| Modelo de revisão de fatos (2ª passada) | env `FACTCHECK_MODEL` (padrão: `claude-sonnet-5`) |
| Modelo de agrupamento/classificação | env `CLUSTER_MODEL` (padrão: `claude-sonnet-5`) |
| Tom / regras do texto | `SYSTEM_PROMPT` em `pipeline/generate.py` |
| Regras de agrupamento por tema | `CLUSTER_SYSTEM_PROMPT` em `pipeline/generate.py` |
| Regras de checagem de fatos | `FACTCHECK_SYSTEM_PROMPT` em `pipeline/generate.py` |
| Nome e visual do site | `site/hugo.toml` e `site/static/css/style.css` |
| Termo de busca de foto por categoria | `CATEGORY_STOCK_QUERIES` em `pipeline/generate.py` |

## Custos
- **GitHub Actions** e **Cloudflare Pages**: cabem no plano gratuito para esse uso.
- **API do Claude**: cada grupo de matéria agora gera **três** chamadas —
  agrupamento/classificação (Sonnet, uma por rodada inteira, não por matéria),
  Haiku (geração) e Sonnet (revisão de fatos). Ainda fica em poucos centavos
  por matéria. Ative o **Batch API** (50% mais barato) se quiser reduzir ainda
  mais — notícia não precisa ser instantânea. Confira o preço atual em
  <https://docs.claude.com>.

## Importante — direitos autorais
Fato não tem direito autoral, mas a **expressão** (texto, estrutura e principalmente
**fotos**) tem. Este projeto trabalha só com o texto das fontes, reescreve com foco
nos fatos e credita o(s) veículo(s) de origem pelo nome no topo da matéria — mas
isso não é aconselhamento jurídico. Não republique fotos das fontes sem licença,
e quanto mais curadoria e conteúdo próprio você acrescentar, mais seguro (e melhor
para SEO/monetização) fica o portal.
