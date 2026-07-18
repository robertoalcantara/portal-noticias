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

### Imagens: variação por IA da imagem-fonte (principal) → Unsplash → Pexels (reservas)
Antes de qualquer banco de fotos, `get_ai_variation_image` (em
`pipeline/generate.py`) tenta usar a própria imagem da matéria no site de
origem: baixa essa imagem e pede ao Gemini (modelo "Nano Banana",
`GEMINI_IMAGE_MODEL`) para gerar uma **variação** dela — muda um pouco o
ângulo das pessoas e dos carros visíveis, sem alterar o contexto geral
(prompt fixo em `IMAGE_VARIATION_PROMPT`). A imagem gerada é salva em
`site/static/images/ia/` e commitada pelo workflow junto com as matérias.

Isso só é tentado se `GEMINI_API_KEY` estiver configurada e a matéria-fonte
tiver uma imagem (`og:image`) que o `trafilatura` consiga extrair. Qualquer
falha nessa etapa (sem imagem na fonte, sem chave, erro de rede/API) é
silenciosa e cai para o banco de fotos genérico, exatamente como já
funcionava antes:

Cada matéria tenta, na ordem, `fetch_unsplash_image` e depois
`fetch_pexels_image` (lista `STOCK_IMAGE_PROVIDERS` em
`pipeline/generate.py`) por um termo genérico da categoria
(`CATEGORY_STOCK_QUERIES`; não dá pra achar foto do evento específico da
matéria, então a busca é por categoria mesmo). Se nada disso achar nada
(ou nenhuma chave estiver configurada), a matéria fica **sem imagem** e o
template usa o placeholder colorido por categoria.

**Unsplash exige atribuição** (diferente do Pexels) — as diretrizes da API
deles pedem: hotlink direto (nunca rehost — já é o que fazemos), crédito
visível ao fotógrafo com link pro perfil, e um aviso à API deles de que a
foto foi "usada" (endpoint `download_location`, chamado automaticamente
por `trigger_unsplash_download` sempre que uma foto do Unsplash é
escolhida). O crédito só aparece na página da matéria (`single.html`),
abaixo da imagem principal — não nos cards pequenos da home/listas, pra
não poluir o grid. Isso é uma escolha de design, não está 100% alinhado
com "atribuição em toda instância" que as diretrizes do Unsplash sugerem
como ideal, mas é o compromisso prático adotado aqui. A imagem gerada por
IA não tem crédito (não é uma foto de banco, é uma variação derivada da
imagem da própria matéria-fonte).

Secrets:
- `GEMINI_API_KEY` — opcional, mas é o que liga a variação por IA. Crie em
  <https://aistudio.google.com/apikey> (Google AI Studio, gratuito para
  uso leve). Sem essa chave, o pipeline pula direto para Unsplash/Pexels.
- `UNSPLASH_ACCESS_KEY` — pexels.com/api tem NADA a ver, é
  unsplash.com/developers → sua app → Access Key. **Não precisa da
  Secret Key** do Unsplash — ela só serve pra fluxos de OAuth em nome de
  usuário, que não usamos aqui (só busca pública).
- `PEXELS_API_KEY` — reserva, tentado só se a variação por IA e o Unsplash
  não acharem nada.

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

### 2b. (Opcional) Variação de imagem por IA (recomendado)
Mesmo caminho de secret:
- `GEMINI_API_KEY` — crie em <https://aistudio.google.com/apikey>
  (Google AI Studio, gratuito para uso leve). Liga a geração de variação
  da imagem da matéria-fonte via Gemini "Nano Banana" (ver seção
  "Imagens" acima). Sem essa chave, o pipeline vai direto para o banco de
  fotos abaixo.

### 2c. (Opcional) Chaves de banco de fotos (reserva)
Mesmo caminho de secret:
- `UNSPLASH_ACCESS_KEY` — principal. Crie em
  <https://unsplash.com/developers> → New Application → Access Key
  (gratuito). Não precisa da Secret Key.
- `PEXELS_API_KEY` — reserva, tentado só se o Unsplash não achar nada.
  Crie em <https://www.pexels.com/api/> (gratuito, instantâneo).

Sem nenhuma dessas chaves, as matérias ficam sem foto (usa o placeholder
colorido por categoria) — não quebra nada.

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
| Prompt da variação de imagem por IA | `IMAGE_VARIATION_PROMPT` em `pipeline/generate.py` |
| Modelo de geração/edição de imagem (IA) | env `GEMINI_IMAGE_MODEL` (padrão: `gemini-2.5-flash-image`) |

## Custos
- **GitHub Actions** e **Cloudflare Pages**: cabem no plano gratuito para esse uso.
- **API do Claude**: cada grupo de matéria agora gera **três** chamadas —
  agrupamento/classificação (Sonnet, uma por rodada inteira, não por matéria),
  Haiku (geração) e Sonnet (revisão de fatos). Ainda fica em poucos centavos
  por matéria. Ative o **Batch API** (50% mais barato) se quiser reduzir ainda
  mais — notícia não precisa ser instantânea. Confira o preço atual em
  <https://docs.claude.com>.
- **API do Gemini (variação de imagem)**: uma chamada de geração/edição de
  imagem por matéria, só quando a matéria-fonte tem imagem e `GEMINI_API_KEY`
  está configurada. Confira o preço atual do modelo (`GEMINI_IMAGE_MODEL`) em
  <https://ai.google.dev/gemini-api/docs/pricing>.

## Importante — direitos autorais
Fato não tem direito autoral, mas a **expressão** (texto, estrutura e principalmente
**fotos**) tem. Este projeto trabalha só com o texto das fontes, reescreve com foco
nos fatos e credita o(s) veículo(s) de origem pelo nome no topo da matéria — mas
isso não é aconselhamento jurídico. Não republique fotos das fontes sem licença,
e quanto mais curadoria e conteúdo próprio você acrescentar, mais seguro (e melhor
para SEO/monetização) fica o portal.
