# GRID — portal de notícias de automobilismo (automatizado)

Pipeline que lê feeds RSS de portais de automobilismo, reescreve cada matéria em
português usando a **API do Claude** (Haiku), revisa os fatos com um segundo
passe usando um **modelo mais forte (Sonnet)** e publica um site estático
(**Hugo**) que se atualiza sozinho. Sem servidor para manter.

```
feeds RSS → Haiku reescreve → Sonnet revisa fatos → Markdown → Hugo → site publicado
                    (GitHub Actions, a cada 3h)                (Cloudflare Pages / Netlify)
```

## Estrutura

```
pipeline/
  sources.yaml        lista de feeds (edite aqui para trocar fontes)
  generate.py         lê feeds, reescreve com o Claude, grava Markdown
  requirements.txt    dependências Python
  seen.json           controle de matérias já publicadas (não apague)
.github/workflows/
  update.yml          agenda a cada 3h + botão "Run workflow"
site/
  hugo.toml           config do site (troque baseURL após publicar)
  layouts/            templates
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
| Trocar/adicionar fontes | `pipeline/sources.yaml` |
| Matérias por feed a cada rodada | `MAX_PER_FEED` (workflow ou env), padrão 5 |
| Frequência de atualização | linha `cron` em `.github/workflows/update.yml` |
| Modelo de geração (1ª passada) | env `MODEL` (padrão: `claude-haiku-4-5-20251001`) |
| Modelo de revisão de fatos (2ª passada) | env `FACTCHECK_MODEL` (padrão: `claude-sonnet-5`) |
| Tom / regras do texto | `SYSTEM_PROMPT` em `pipeline/generate.py` |
| Regras de checagem de fatos | `FACTCHECK_SYSTEM_PROMPT` em `pipeline/generate.py` |
| Nome e visual do site | `site/hugo.toml` e `site/static/css/style.css` |

## Custos
- **GitHub Actions** e **Cloudflare Pages**: cabem no plano gratuito para esse uso.
- **API do Claude**: cada matéria agora gera **duas** chamadas — Haiku (geração) +
  Sonnet (revisão de fatos). O Sonnet é mais caro por token, mas o volume de
  texto por matéria é pequeno, então o custo total ainda fica em poucos
  centavos por matéria. Ative o **Batch API** (50% mais barato) se quiser
  reduzir ainda mais — notícia não precisa ser instantânea. Confira o preço
  atual em <https://docs.claude.com>.

## Importante — direitos autorais
Fato não tem direito autoral, mas a **expressão** (texto, estrutura e principalmente
**fotos**) tem. Este projeto trabalha só com o texto do feed, reescreve com foco nos
fatos e **sempre credita e linka a fonte** — mas isso não é aconselhamento jurídico.
Não republique fotos das fontes sem licença, e quanto mais curadoria e conteúdo
próprio você acrescentar, mais seguro (e melhor para SEO/monetização) fica o portal.
