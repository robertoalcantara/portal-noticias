# Grid Geral — portal de notícias de automobilismo (automatizado)

Pipeline que lê notícias de várias fontes (RSS e sites sem feed), agrupa
manchetes que tratam do mesmo fato vindas de fontes diferentes, reescreve o
resultado em português com a **API do Claude** — ou, se `DEEPSEEK_API_KEY`
estiver configurada, com o **DeepSeek no lugar do Claude** (ver "Ajustes
rápidos") —, revisa os fatos com um segundo passe (Haiku, mesma família do
modelo de geração, ou DeepSeek também) e publica um site estático (**Hugo**)
que se atualiza sozinho. Sem servidor para manter.

Todas as matérias são assinadas por um de dois pseudônimos editoriais, que
o próprio pipeline escolhe automaticamente por matéria (ver
`select_writer()` em `pipeline/generate.py`):
- **Bruno Bandeira** — tom irônico e bem-humorado, usado por padrão.
- **Armando Traço** — tom técnico, objetivo e com humor bem discreto,
  usado quando o grupo tem 3 ou mais textos-fonte utilizáveis (matéria
  com material o bastante pra ir mais fundo no lado técnico).

O sistema é feito pra crescer: novos estilos de editor são adicionados
como um novo `WriterProfile` em `WRITER_PROFILES`, sem mexer em mais
nada do código.

**Escopo:** Kart, F1, F2, F3, GT3, WEC, IndyCar e NASCAR.

```
fontes (RSS + sites sem feed) → agrupar/classificar (Haiku) → Haiku reescreve
        → Haiku revisa fatos → Markdown → Hugo → site publicado
                    (GitHub Actions)                   (Cloudflare Pages)
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
2. `cluster_and_classify()` — UMA chamada ao Claude (Haiku) recebe TODAS as
   manchetes da rodada e devolve grupos: quais manchetes tratam do mesmo
   fato (mesmo vindas de fontes diferentes) e qual categoria cada grupo tem
   — ou `"DESCARTAR"` se estiver fora do escopo do site.
3. Para cada grupo válido: baixa o texto completo de cada fonte do grupo,
   chama `rewrite_with_claude()` (Haiku, gera UMA matéria agregando as
   fontes) e depois `factcheck_with_claude()` (Haiku, compara o rascunho
   contra os textos-fonte e corrige/remove o que não está lá — ver nota
   sobre custo abaixo, essa etapa já rodou em Sonnet antes).
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
- **Matéria publicada cortada no meio de uma frase** já aconteceu:
  `rewrite_with_claude`/`factcheck_with_claude` bateram no limite de
  `max_tokens` (era 4096) no meio do JSON, e `_lenient_json_repair`
  (pensado pra aspas internas não escapadas, não pra truncamento de
  verdade) "salvava" um objeto mesmo assim, com `corpo_markdown`
  incompleto — publicado sem ninguém perceber. Duas correções: (1)
  `_call_anthropic`/`_call_deepseek` agora levantam erro se
  `stop_reason`/`finish_reason` indicar corte por limite de tokens
  (`max_tokens`/`length`), em vez de deixar seguir pro parser tolerante;
  (2) `max_tokens` de escrever/revisar fatos subiu pra 8192, com mais
  folga. Se voltar a acontecer, o log mostra
  "resposta cortada por limite de max_tokens" em vez de publicar
  silenciosamente.
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
- **Modo manual (`MANUAL_URLS`) NÃO passa por `cluster_and_classify`.**
  De propósito — o filtro de "fora do escopo"/"redundante" existe pra
  decidir automaticamente o que vale a pena virar matéria; quando uma
  pessoa já escolheu o link à mão, esse julgamento já foi feito por
  ela. A categoria final da matéria ainda vem normal, do próprio
  `rewrite_with_claude` (o JSON de saída sempre inclui `"categoria"`
  dentre `ALLOWED_CATEGORIES` — isso nunca dependeu do resultado do
  agrupamento, só o filtro de escopo é que dependia). O núcleo do
  processamento (escrita → revisão de fatos → imagem → cards → post)
  foi extraído do loop de `main()` pra `process_group()` justamente
  pra ser reaproveitado nos dois modos sem duplicar lógica — se mexer
  num, teste o outro também (ver testes ponta a ponta feitos com
  `main()`/`run_manual_mode()` mockados, mesmo padrão usado nas
  outras features deste projeto).
- **Cache HTTP: não mexemos no comportamento padrão do Cloudflare Pages
  para HTML/CSS/JS, de propósito.** Por padrão o Pages já envia
  `Cache-Control: public, max-age=0, must-revalidate` + `ETag` pra todo
  asset — isso obriga o navegador a sempre revalidar com o servidor
  antes de reusar uma cópia em cache, então uma matéria nova/editada
  nunca fica escondida atrás de cache do navegador. A própria Cloudflare
  recomenda evitar cache customizado quando o padrão já resolve. O único
  `_headers` que criamos (`site/static/_headers`, copiado pro `public/`
  pelo build do Hugo) é pra `/images/ia/*` e `/images/cards/*`
  (`Cache-Control: public, max-age=31536000, immutable`) — o nome de
  cada arquivo nessas pastas é derivado de um hash ou de um slug fixo
  por matéria já publicada (nunca reeditada), então o conteúdo daquele
  nome nunca muda e dá pra cachear "pra sempre" sem risco de ficar
  desatualizado. Se quiser confirmar o comportamento ao vivo, inspecione
  os headers de resposta do site publicado (`curl -I`) — isso não dá
  pra verificar de dentro deste sandbox porque `api.cloudflare.com` e o
  domínio publicado do site estão fora da allowlist de rede daqui.

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

### Contexto de matérias já publicadas (evita notícia redundante/superada)
Antes de agrupar/classificar as manchetes novas de cada rodada, o pipeline
lê as matérias já publicadas em `site/content/posts/` nas últimas
`RECENT_CONTEXT_WINDOW_HOURS` horas (padrão 72h, teto de
`RECENT_CONTEXT_MAX_ITEMS` matérias, padrão 60 — ver
`load_recent_published_context()` em `pipeline/generate.py`) e passa essa
lista (título, categoria, há quanto tempo) como um bloco de CONTEXTO à
parte no início do prompt de `cluster_and_classify`. A regra 6 do
`CLUSTER_SYSTEM_PROMPT` instrui o modelo a marcar como `"DESCARTAR"`
qualquer manchete nova que cubra uma etapa ANTERIOR do MESMO evento que
uma matéria já publicada (mais recente e mais avançada) já cobre — por
exemplo, não faz sentido publicar o resultado da classificação de uma
etapa depois que o resultado da corrida daquela mesma etapa já saiu. O
modelo só descarta por esse motivo quando dá pra confirmar que é o mesmo
evento numa fase mais avançada; na dúvida, não descarta.

### Imagens: variação por IA da imagem-fonte (única fonte de imagem)
`get_ai_variation_image` (em `pipeline/generate.py`) usa a própria imagem
da matéria no site de origem: baixa essa imagem e pede ao Gemini (modelo
"Nano Banana", `GEMINI_IMAGE_MODEL`) para gerar uma **variação** dela —
muda um pouco o ângulo das pessoas e dos carros visíveis, sem alterar o
contexto geral (prompt fixo em `IMAGE_VARIATION_PROMPT`). A imagem gerada
é salva em `site/static/images/ia/` e commitada pelo workflow junto com as
matérias.

Não há mais banco de fotos genérico como reserva — Unsplash e Pexels foram
removidos de propósito. Se `GEMINI_API_KEY` não estiver configurada ou a
matéria-fonte não tiver uma imagem (`og:image`) que o `trafilatura`
consiga extrair (e nenhuma outra fonte do grupo tiver), a matéria fica
**sem imagem** e o template usa o placeholder colorido por categoria.

Se a foto original for baixada com sucesso mas a **chamada ao Gemini
falhar** (rede, quota, resposta inválida etc.), o pipeline usa a **mesma
foto original da matéria-fonte, sem edição**, em vez de tentar outra
fonte do grupo ou ficar sem imagem — só nesse caso a foto é creditada
(`image_credit_name`/`image_credit_url`, mostrado na página da matéria).
O crédito prioriza o crédito ESPECÍFICO da fotografia, se a página da
matéria-fonte trouxer um (`extract_photo_credit()` em
`pipeline/generate.py`: procura dados estruturados schema.org
`ImageObject` — `creditText`/`copyrightHolder`/`creator` — ou uma
legenda `<figcaption>` com palavra-chave de crédito, tipo "Foto:",
"Crédito:", "Divulgação", nome de agência etc.); só cai pro nome do
veículo quando a página não traz nada mais específico. A imagem gerada
por IA (quando a geração dá certo) **nunca** leva crédito, já que é uma
variação derivada, não uma foto de banco nem a foto de terceiros.

Secrets:
- `GEMINI_API_KEY` — necessária para ter imagem nas matérias. Crie em
  <https://aistudio.google.com/apikey> (Google AI Studio). Sem essa chave,
  todas as matérias saem sem imagem.

### Cards para Stories do Instagram (gerados por matéria)
Depois que uma matéria é escrita, o pipeline chama `generate_and_render_cards()`
(em `pipeline/generate.py`) que: (1) pede ao modelo de texto ativo (Claude ou
DeepSeek, o que estiver configurado — env opcional `CARDS_MODEL`, padrão
`claude-haiku-4-5-20251001`, ignorado se o DeepSeek estiver ativo) para
resumir a matéria em **1 a 5 textos curtos**, no mesmo tom do escritor que
assinou aquela matéria (Bruno Bandeira ou Armando Traço), um por card; (2) renderiza cada texto como uma imagem 1080×1920 (formato
Stories) com **Pillow puro** (`pipeline/cards.py`, sem navegador/headless
nenhum) usando a própria imagem da matéria como fundo (ou o placeholder
colorido por categoria, se a matéria não tiver imagem). As fontes usadas
(Fraunces, Archivo Black, Barlow Condensed) estão em `pipeline/assets/fonts/`,
todas licenciadas OFL (arquivos `OFL-*.txt` ao lado de cada uma).

Isso roda **automaticamente para toda matéria nova**, sem intervenção manual.
Se a geração dos cards falhar por qualquer motivo (resposta do modelo
inválida, erro de renderização, etc.), o erro é só um aviso — a matéria é
publicada normalmente, **sem** cards, em vez de falhar a rodada inteira.

As imagens ficam em `site/static/images/cards/<slug-da-matéria>/1.png`...`N.png`
e uma página em Hugo é gerada em `<url-da-matéria>/cards/` (mesmo slug,
seção `site/content/cards/`, permalink alinhado via `[permalinks]` em
`site/hugo.toml`) com a galeria pra visualizar/baixar cada card. Essa página
tem `noindex` (não deve aparecer em buscadores — ver `head.html`) e só existe
um link pra ela na matéria (`Ver cards para Stories →`) quando
`has_cards = true` no frontmatter do post.

O texto de cada card é sobreposto direto na foto de fundo (sem controle
sobre o que vai aparecer atrás — a foto muda por matéria), então precisa de
contraste garantido independente de foto clara, escura ou cheia de
detalhe. Já testamos contorno escuro em volta das letras
(`stroke_width`/`stroke_fill` do Pillow) e o resultado ficou ruim
visualmente — foi revertido. A solução atual, em `render_card()`
(`pipeline/cards.py`):
- `_gradient_overlay()` mais forte que o original: o véu plano por cima de
  toda a foto subiu de alpha 46 pra 92, e o gradiente do rodapé cobre uma
  faixa maior (72% da altura do card, era 62%) — o suficiente pra escurecer
  a foto até em cards com texto longo (várias linhas), sem precisar de
  contorno.
- O título principal do card usa a cor laranja da marca (`DEFAULT_COLOR`,
  `#e4572e` — a mesma dos links "Baixar ↓" na página de cards, ver
  `.cards-item figcaption a` em `site/static/css/style.css`) em vez de
  branco liso — maior contraste e reforça a identidade visual, sem
  contorno. Marca "Grid Geral" e handle do rodapé continuam brancos (ficam
  numa faixa onde o véu/gradiente já garante contraste de sobra).

## Estrutura

```
pipeline/
  sources.yaml        fontes: RSS ou sites sem feed (raspagem de listagem)
  generate.py          coleta, agrupa, gera e revisa as matérias
  cards.py             renderiza os cards de Stories (Pillow, sem rede)
  publish_instagram.py publica os cards já gerados como Stories + feed no Instagram
  assets/fonts/        fontes usadas nos cards (OFL)
  requirements.txt    dependências Python
  seen.json           controle de matérias já publicadas (não apague)
  instagram_posted.json controle de Stories já publicadas (não apague)
  instagram_feed_posted.json controle de posts de feed já publicados (não apague)
.github/workflows/
  update.yml          agenda a cada 3h + botão "Run workflow"
  instagram-stories.yml publica Stories + feed pendentes no Instagram
site/
  hugo.toml           config do site (troque baseURL após publicar)
  layouts/            templates (home tipo portal, cards com cor por categoria)
  static/css/         estilo
  content/posts/      matérias geradas (começa vazio)
  content/cards/      páginas da galeria de cards (uma por matéria com cards)
  static/images/cards/ imagens dos cards renderizados (PNG, 1080×1920)
```

## Como colocar no ar (passo a passo)

### 1. Suba o projeto no GitHub
Crie um repositório novo e envie estes arquivos.

### 2. Guarde a chave da API do Claude
No repositório: **Settings → Secrets and variables → Actions → New repository secret**
- Nome: `ANTHROPIC_API_KEY`
- Valor: sua chave (crie em <https://console.anthropic.com/>)

### 2b. (Opcional, mas recomendado) Variação de imagem por IA
Mesmo caminho de secret:
- `GEMINI_API_KEY` — crie em <https://aistudio.google.com/apikey>
  (Google AI Studio). Liga a geração de variação da imagem da
  matéria-fonte via Gemini "Nano Banana" (ver seção "Imagens" acima).

Sem essa chave, as matérias saem sem foto (usa o placeholder colorido por
categoria) — não quebra nada. Não há mais banco de fotos genérico como
reserva (Unsplash/Pexels foram removidos).

### 2c. (Opcional) DeepSeek em vez do Claude para o texto
Mesmo caminho de secret:
- `DEEPSEEK_API_KEY` — crie em <https://platform.deepseek.com/api_keys>.

Se essa secret existir, o DeepSeek passa a ser o **único** modelo usado
nas três chamadas de texto (agrupamento, geração e revisão de fatos) —
o Claude simplesmente não é chamado. Não há fallback: se o DeepSeek
falhar (rede, quota, resposta vazia ou inválida), o erro sobe normalmente
(a matéria/rodada falha e tenta de novo depois), em vez de cair pro
Claude. Pra voltar a usar o Claude, **apague a secret `DEEPSEEK_API_KEY`**
— nesse caso `ANTHROPIC_API_KEY` volta a ser obrigatória.

Todas as chamadas ao DeepSeek usam o modo **JSON Output** nativo da API
(`response_format: {"type": "json_object"}`) — reduz bastante o risco de
JSON malformado/truncado (a causa mais comum de falha na revisão de
fatos e nas outras etapas). A própria doc do DeepSeek avisa que esse
modo pode ocasionalmente devolver conteúdo vazio (bug conhecido deles);
quando isso acontece, a etapa falha e a matéria segue sem essa correção
específica (nunca derruba a rodada inteira) — ver `_call_deepseek()` em
`pipeline/generate.py`.

Os nomes `deepseek-chat`/`deepseek-reasoner` (usados como padrão até
pouco tempo atrás) serão descontinuados pela DeepSeek em 24/07/2026 —
o padrão agora é `deepseek-v4-flash` (o sucessor direto, sem mudança de
comportamento). Se quiser usar o modelo mais forte (e mais caro) pra
alguma etapa específica, `deepseek-v4-pro` também está disponível via
`DEEPSEEK_MODEL` (troca as três chamadas de uma vez — não tem, hoje, uma
env separada só pra revisão de fatos).

### 3. Hospedagem: Cloudflare Pages (já automatizado)
O próprio workflow do GitHub Actions builda o site com Hugo e publica no
Cloudflare Pages a cada rodada — não depende de conectar o repositório pela
interface do Cloudflare. Para isso, dois secrets adicionais no repositório:
- `CLOUDFLARE_API_TOKEN` — token customizado com permissão de conta
  **Cloudflare Pages: Edit** (crie em dash.cloudflare.com/profile/api-tokens →
  Create Custom Token)
- `CLOUDFLARE_ACCOUNT_ID` — ID da conta (aparece na URL do dashboard)

Este projeto já está publicado em: **https://gridgeral.com/** (domínio
próprio, configurado como Custom domain no projeto Cloudflare Pages
`portal-noticias` — `www.gridgeral.com` também aponta pra lá e redireciona
pro domínio raiz). O endereço original do projeto,
`https://portal-noticias-cz7.pages.dev/`, continua funcionando também. Se
trocar de domínio no futuro, ajuste `baseURL` em `site/hugo.toml`.

### 4. Gere as primeiras matérias
No GitHub: aba **Actions → "Atualizar notícias" → Run workflow**.
O script roda, grava as matérias, faz commit — e o Cloudflare publica.
Depois disso, roda sozinho a cada 3 horas.

### 5. (Opcional) Gerar uma matéria manualmente a partir de link(s)
Além da rodada automática, tem um segundo workflow —
**Actions → "Gerar matéria manual (link específico)" → Run workflow**
— que recebe um campo `urls` (um link por linha, ou separados por
vírgula) e gera UMA matéria só a partir deles, pulando sources.yaml e o
agrupamento/classificação automático por completo. Útil pra publicar
algo na hora (ex.: saiu uma notícia importante e você não quer esperar
a próxima rodada de hora em hora varrer o RSS/listagem da fonte).

Se dois ou mais links tratarem do MESMO fato, informe todos juntos no
mesmo campo `urls`: eles viram fontes de UMA única matéria (mesma
lógica de agregação multi-fonte do funil automático), em vez de gerar
uma matéria por link. Passa pelo mesmo pipeline de sempre — escrita,
revisão de fatos, imagem, cards — só que sem o filtro de "fora do
escopo"/"redundante" do agrupamento automático (a pessoa que
escolheu o link já decidiu que aquilo deve virar matéria). Os links
processados são marcados em `pipeline/seen.json` no final, pra rodada
automática não tentar publicar a mesma notícia de novo depois.

Tem também um campo opcional `images` — link(s) de imagem separados por
vírgula, pra usar na matéria em vez de tentar extrair foto da(s)
página(s)-fonte. A PRIMEIRA imagem é a capa (passa pela mesma variação
por IA de sempre se `GEMINI_API_KEY` estiver configurada, com o mesmo
fallback pra imagem original se a API falhar ou a chave não estiver
definida); as demais, se houver, são usadas como fundo dos cards de
Stories, uma por card, alternando em ordem em vez de repetir sempre a
mesma foto da capa. Deixe `images` em branco pra manter o comportamento
de sempre (extrair a foto automaticamente da matéria-fonte).

Ver env `MANUAL_URLS`/`MANUAL_IMAGES`, funções `run_manual_mode()` e
`build_manual_image_info()` em `pipeline/generate.py`, e o workflow
`.github/workflows/manual-article.yml`.

### 6. (Opcional) Excluir uma matéria pelo link
**Actions → "Excluir matéria (por link)" → Run workflow**, campo
`url` com o link da matéria publicada — pode ser o link completo
(`https://gridgeral.com/2026/07/titulo-da-materia/`) ou só o caminho
a partir do domínio (`/2026/07/titulo-da-materia`), com ou sem barra
no início/fim. Apaga o post, a página de cards de Stories (se
houver) e as imagens geradas associadas (capa e/ou cards) — só apaga
uma imagem se nenhuma OUTRA matéria ainda a referenciar (checagem de
segurança; ver `run_delete_mode()`). NÃO mexe em
`pipeline/seen.json`: apagar uma matéria não faz o funil automático
tentar publicá-la de novo sozinho — se quiser isso, é um passo à
parte. Esse workflow não chama nenhuma API de LLM/imagem, então não
precisa das chaves de API configuradas nele.

O link é casado pelo SLUG (o último trecho da URL) contra os
arquivos em `site/content/posts/` — se não achar uma correspondência
EXATA, a exclusão é cancelada (retorna erro, com sugestões de slugs
parecidos no log) em vez de arriscar apagar a matéria errada. Também
tolera colar o link da página de cards (`.../slug/cards/`), usar `_`
em vez de `-`, ou digitar o título inteiro quando o slug real foi
cortado em 70 caracteres — ver `find_post_by_url()`/`DELETE_URL` em
`pipeline/generate.py` e o workflow
`.github/workflows/delete-article.yml`.

### 7. (Opcional) Trocar a imagem principal de uma matéria já publicada
**Actions → "Trocar imagem da matéria (por link)" → Run workflow**,
campos `url` (a matéria — mesmas regras da exclusão acima: link
completo ou só o caminho, ex. `/2026/07/titulo-da-materia`) e `image`
(link da nova foto de capa). Troca
só a imagem principal do post (frontmatter `image`/`image_credit_*`),
sem tocar em mais nada — título, corpo, categorias etc. continuam
iguais. A nova imagem passa pela mesma variação por IA de sempre se
`GEMINI_API_KEY` estiver configurada (mesmo fallback pra imagem
original se a API falhar). Se a imagem antiga não for mais usada por
nenhuma outra matéria, o arquivo antigo em `site/static/images/ia/`
é removido.

**Não mexe nos cards de Stories já gerados** — se a matéria já tinha
cards, eles continuam com a imagem antiga (trocar as imagens dos
cards exigiria regerar os cards inteiros, com textos e tudo — fora
do escopo deste modo). Ver env `REPLACE_IMAGE_URL`/
`REPLACE_IMAGE_SOURCE`, função `run_replace_image_mode()` em
`pipeline/generate.py`, e o workflow
`.github/workflows/replace-article-image.yml`.

### 8. (Opcional) Publicação automática no Instagram (Stories + feed)
Publica os cards de cada matéria automaticamente em **@gridgeral**, de
duas formas independentes — workflow separado
(`.github/workflows/instagram-stories.yml`), agendado pra rodar depois
do funil principal (dá tempo do deploy do Cloudflare Pages propagar; a
API do Instagram busca a imagem por URL pública, ela precisa estar no
ar):
- **Stories:** cada card vira um Story separado (um Story por imagem).
- **Feed:** todos os cards da matéria viram um único post de
  **carrossel** (ou uma imagem única, se a matéria só tiver 1 card),
  com legenda montada a partir do título/resumo/categoria da matéria e
  um "link na bio" (Instagram não permite link clicável na legenda).

Não depende de qual workflow gerou a matéria (funil automático, modo
manual etc.) — só varre o que já está publicado e posta o que ainda
não foi, em cada um dos dois formatos, de forma independente (um pode
ficar pra trás sem travar o outro).

**Pré-requisito (feito manualmente, uma vez só, direto no painel da
Meta — isso aqui ninguém automatiza por você).** Usamos o fluxo
**"Instagram API with Instagram Login"** (a Meta também tem um fluxo
mais antigo via Página do Facebook — "Instagram API with Facebook
Login" — mas o de cima é o mais simples: **não exige vincular uma
Página do Facebook**):
1. A conta do Instagram (@gridgeral) precisa ser **Business** ou
   **Creator** (Configurações → Conta → Mudar para conta
   profissional, no app do Instagram) — sem precisar linkar Página.
2. Crie um app em <https://developers.facebook.com/apps/> (tipo
   "Business"), adicione o produto **Instagram** (o card
   "Instagram API with Instagram Login" / "Business Login for
   Instagram") a ele.
3. Gere um **token de acesso de longa duração** (~60 dias, renovável)
   com as permissões `instagram_business_basic` e
   `instagram_business_content_publish` — pelo fluxo de login do
   próprio app (Business Login for Instagram) ou pelo Graph API
   Explorer, gerando primeiro um token de curta duração (1h) e
   trocando por um de longa duração em
   `GET https://graph.instagram.com/access_token?grant_type=ig_exchange_token&client_secret=<APP_SECRET>&access_token=<TOKEN_CURTO>`
   (ver <https://developers.facebook.com/docs/instagram-platform/reference/access_token/>,
   a Meta pode mudar esse fluxo de vez em quando, vale conferir a doc
   atual). O token gerado por esse fluxo começa com `IGA...`.
4. Descubra o **ID numérico da conta do Instagram** (não é o
   `@usuario`):
   `GET https://graph.instagram.com/v21.0/me?fields=id,username&access_token=<TOKEN>`.

Com isso em mãos, dois secrets no repositório (mesmo caminho dos
outros: **Settings → Secrets and variables → Actions**):
- `INSTAGRAM_ACCESS_TOKEN` — o token de longa duração do passo 3
  (começa com `IGA...`).
- `INSTAGRAM_BUSINESS_ACCOUNT_ID` — o ID numérico do passo 4.

Sem esses dois secrets configurados, o workflow roda e não faz nada
(sai de propósito sem erro — mesmo padrão de `GEMINI_API_KEY`
ausente).

**Atenção ao host da API:** um token do fluxo "Instagram Login"
(`IGA...`) só funciona contra `graph.instagram.com` — chamar
`graph.facebook.com` com esse token dá erro 190 "Cannot parse access
token" (é o host do OUTRO fluxo, o antigo via Página do Facebook,
com token `EAA...`). `pipeline/publish_instagram.py` já usa o host
certo (`graph.instagram.com`) pro token desse passo a passo; se um
dia trocar pro fluxo antigo (Página do Facebook), o host também
precisa mudar junto.

**Corte de segurança:** hoje o Instagram limita a API a 100
publicações por conta a cada 24h, somando TODOS os tipos de conteúdo
publicado via API — Stories, feed, reels (um carrossel conta como 1
publicação, não uma por imagem; ver
<https://developers.facebook.com/docs/instagram-platform/content-publishing>
pro número atual, a Meta já mudou esse limite antes; posts feitos à
mão pelo app do Instagram não contam nesse limite). Ficamos BEM abaixo
de propósito, com cotas SEPARADAS pra cada formato: no máximo
`MAX_INSTAGRAM_POSTS_PER_DAY` (padrão 20) Stories e
`MAX_INSTAGRAM_FEED_POSTS_PER_DAY` (padrão 10) posts de feed, por
rodada — 30 no total, bem abaixo dos 100, mesmo que as duas cotas
batam o teto no mesmo dia. Matérias com mais de 48h não geram mais
Stories nem post de feed novo (conteúdo "do agora" — não faz sentido
postar algo de dias atrás só porque sobrou cota). O controle do que já
foi publicado fica em `pipeline/instagram_posted.json` (Stories) e
`pipeline/instagram_feed_posted.json` (feed) — não apague nenhum dos
dois, sem eles o script tentaria republicar tudo de novo.

Ver `pipeline/publish_instagram.py` pros detalhes de implementação
(Instagram API: criar container de mídia → esperar processar →
publicar; carrossel usa `is_carousel_item` por imagem + um container
"pai" com `children`).

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
| Modelo de revisão de fatos (2ª passada) | env `FACTCHECK_MODEL` (padrão: `claude-haiku-4-5-20251001`) |
| Modelo de agrupamento/classificação | env `CLUSTER_MODEL` (padrão: `claude-haiku-4-5-20251001`) |
| Usar só o DeepSeek em vez do Claude (sem fallback) | env `DEEPSEEK_API_KEY` (apague pra voltar ao Claude) |
| Modelo do DeepSeek | env `DEEPSEEK_MODEL` (padrão: `deepseek-v4-flash`; `deepseek-v4-pro` é a opção mais forte/cara) |
| Modelo de geração dos textos dos cards de Stories | env `CARDS_MODEL` (padrão: `claude-haiku-4-5-20251001`, ignorado se DeepSeek ativo) |
| Tom / regras do texto | `SYSTEM_PROMPT` em `pipeline/generate.py` |
| Regras de agrupamento por tema | `CLUSTER_SYSTEM_PROMPT` em `pipeline/generate.py` |
| Contexto de matérias já publicadas (evita redundância) | env `RECENT_CONTEXT_WINDOW_HOURS` (padrão 72h) e `RECENT_CONTEXT_MAX_ITEMS` (padrão 60) em `pipeline/generate.py` |
| Regras de checagem de fatos | `FACTCHECK_SYSTEM_PROMPT` em `pipeline/generate.py` |
| Nome e visual do site | `site/hugo.toml` e `site/static/css/style.css` |
| Prompt da variação de imagem por IA | `IMAGE_VARIATION_PROMPT` em `pipeline/generate.py` |
| Modelo de geração/edição de imagem (IA) | env `GEMINI_IMAGE_MODEL` (padrão: `gemini-2.5-flash-image`) |
| Escritores/pseudônimos e critério de escolha | `WRITER_PROFILES` e `select_writer()` em `pipeline/generate.py` (padrão: Bruno Bandeira; Armando Traço a partir de 3 fontes utilizáveis) |
| Cache HTTP (Cache-Control) das imagens de matéria | `site/static/_headers` (padrão do Cloudflare Pages já cobre HTML/CSS/JS) |
| Gerar matéria manual a partir de link(s) específico(s) | workflow `manual-article.yml` (campo `urls`) ou env `MANUAL_URLS` local |
| Imagem manual da matéria (capa + extras pros cards) | campo `images` do workflow manual, ou env `MANUAL_IMAGES` local |
| Excluir matéria pelo link | workflow `delete-article.yml` (campo `url`) ou env `DELETE_URL` local |
| Trocar imagem principal de matéria já publicada | workflow `replace-article-image.yml` (campos `url`/`image`) ou envs `REPLACE_IMAGE_URL`/`REPLACE_IMAGE_SOURCE` locais |
| Publicação automática de Stories no Instagram | workflow `instagram-stories.yml`, secrets `INSTAGRAM_ACCESS_TOKEN`/`INSTAGRAM_BUSINESS_ACCOUNT_ID`, env `MAX_INSTAGRAM_POSTS_PER_DAY` (padrão 20) |

## Custos
- **GitHub Actions** e **Cloudflare Pages**: cabem no plano gratuito para esse uso.
- **API do Claude**: cada grupo de matéria agora gera **três** chamadas —
  agrupamento/classificação (uma por rodada inteira, não por matéria),
  geração e revisão de fatos — hoje as três em **Haiku**, o modelo mais
  barato disponível. A revisão de fatos já rodou em Sonnet (mais caro, mas
  um modelo independente checando o rascunho); trocamos para Haiku pra
  cortar custo, o que reduz um pouco a força dessa segunda checagem — se
  notar mais erros factuais passando batido, considere voltar
  `FACTCHECK_MODEL` para `claude-sonnet-5`. Ative o **Batch API** (50% mais
  barato) se quiser reduzir ainda mais — notícia não precisa ser
  instantânea. Confira o preço atual em <https://docs.claude.com>.
- **DeepSeek** (opcional): se `DEEPSEEK_API_KEY` estiver configurada, as
  mesmas três chamadas usam só o DeepSeek (custo por token bem menor que o
  Claude), sem fallback — o Claude não é chamado enquanto essa secret
  existir. Confira o preço atual em
  <https://api-docs.deepseek.com/quick_start/pricing>.
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
