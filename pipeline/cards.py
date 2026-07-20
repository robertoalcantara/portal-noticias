#!/usr/bin/env python3
"""
Renderiza os "cards" de Stories (Instagram) de uma matéria: imagens verticais
(1080x1920) com a foto principal da matéria como fundo e um texto curto por
cima — pensadas pra serem baixadas e postadas manualmente no Stories.

Não faz nenhuma chamada de rede/API — só recebe textos já prontos (gerados
em generate.py via call_llm, ver CARDS_SYSTEM_PROMPT) e uma imagem de fundo
opcional, e desenha com Pillow. Se não houver imagem de fundo (matéria sem
foto), usa um fundo sólido na cor da categoria com a mesma listra diagonal
do placeholder do site (ver .placeholder em site/static/css/style.css) —
assim os cards continuam com cara de BRGrid mesmo sem foto.

Qualquer falha aqui (fonte não carrega, imagem corrompida etc.) deve ser
tratada pelo chamador como não-fatal: sem cards, a matéria continua sendo
publicada normalmente — ver generate.py.
"""

from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONTS_DIR = Path(__file__).resolve().parent / "assets" / "fonts"

CARD_W, CARD_H = 1080, 1920

# Mesmas cores por categoria do site (site/static/css/style.css, :root).
CATEGORY_COLORS = {
    "Kart": "#2e8b57",
    "F1": "#e4572e",
    "F2": "#3a6ea5",
    "F3": "#8e44ad",
    "F4": "#c9a227",
    "GT3": "#1590a6",
    "WEC": "#4a4e9e",
    "Indy": "#d1495b",
    "NASCAR": "#b8790a",
}
DEFAULT_COLOR = "#e4572e"  # fallback (mesma cor do F1) se a categoria não for reconhecida

_FONT_CACHE: dict[tuple[str, int], ImageFont.FreeTypeFont] = {}


def _load_font(name: str, size: int, variation: bytes | None = None) -> ImageFont.FreeTypeFont:
    """Carrega (e cacheia) uma fonte TTF de pipeline/assets/fonts. `name` é o
    arquivo (sem extensão) dentro dessa pasta. `variation`, se dado, seleciona
    uma instância nomeada de uma fonte variável (ex.: b"Bold")."""
    key = (name, size)
    font = _FONT_CACHE.get(key)
    if font is None:
        font = ImageFont.truetype(str(FONTS_DIR / f"{name}.ttf"), size)
        _FONT_CACHE[key] = font
    if variation:
        try:
            font.set_variation_by_name(variation)
        except Exception:  # noqa: BLE001 — fonte estática (sem eixo de variação): ignora
            pass
    return font


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    hex_color = hex_color.lstrip("#")
    return tuple(int(hex_color[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _cover_fit(img: Image.Image, target_w: int, target_h: int) -> Image.Image:
    """Redimensiona + corta (mantendo proporção) pra preencher target_w x
    target_h por inteiro, cortando o excesso — mesma lógica do `object-fit:
    cover` do CSS usado no resto do site."""
    src_w, src_h = img.size
    src_ratio = src_w / src_h
    target_ratio = target_w / target_h
    if src_ratio > target_ratio:
        # fonte mais "larga" que o alvo: ajusta pela altura, corta os lados
        new_h = target_h
        new_w = round(new_h * src_ratio)
    else:
        new_w = target_w
        new_h = round(new_w / src_ratio)
    resized = img.resize((new_w, new_h), Image.LANCZOS)
    left = (new_w - target_w) // 2
    top = (new_h - target_h) // 2
    return resized.crop((left, top, left + target_w, top + target_h))


def _diagonal_placeholder(color_hex: str) -> Image.Image:
    """Fundo sólido com listra diagonal clara, mesma linguagem visual do
    .placeholder do site — usado quando a matéria não tem foto."""
    base = Image.new("RGB", (CARD_W, CARD_H), _hex_to_rgb(color_hex))
    stripes = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(stripes)
    stripe_w = 26
    gap = 30
    # linhas diagonais grossas, de canto a canto, espaçadas — desenhadas bem
    # além da borda pra cobrir depois de "girar" via um paralelogramo simples
    x = -CARD_H
    while x < CARD_W + CARD_H:
        draw.polygon(
            [
                (x, CARD_H),
                (x + CARD_H, 0),
                (x + CARD_H + stripe_w, 0),
                (x + stripe_w, CARD_H),
            ],
            fill=(255, 255, 255, 36),
        )
        x += stripe_w + gap
    base = base.convert("RGBA")
    base.alpha_composite(stripes)
    return base.convert("RGB")


def _gradient_overlay(height_fraction: float = 0.72) -> Image.Image:
    """Gradiente preto transparente -> opaco, de mais da metade pra baixo,
    pra garantir contraste do texto independente do brilho da foto (sem
    depender de contorno em volta das letras -- só a foto mais escura ali
    embaixo)."""
    overlay = Image.new("RGBA", (CARD_W, CARD_H), (0, 0, 0, 0))
    grad_h = round(CARD_H * height_fraction)
    start_y = CARD_H - grad_h
    for y in range(grad_h):
        # curva quadrática: começa bem sutil e fica forte perto do rodapé
        t = y / grad_h
        alpha = int(245 * (t ** 1.4))
        ImageDraw.Draw(overlay).line(
            [(0, start_y + y), (CARD_W, start_y + y)], fill=(6, 7, 10, alpha)
        )
    # véu por cima de toda a imagem, pra unificar fotos muito claras/escuras
    # (mais forte que antes -- é a principal defesa de contraste agora que
    # não usamos mais contorno no texto)
    ImageDraw.Draw(overlay).rectangle([0, 0, CARD_W, CARD_H], fill=(6, 7, 10, 92))
    return overlay


def _wrap_to_width(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int
) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if draw.textlength(candidate, font=font) <= max_width or not current:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def _fit_text_block(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_width: int,
    max_height: int,
    start_size: int = 92,
    min_size: int = 44,
) -> tuple[ImageFont.FreeTypeFont, list[str], int]:
    """Acha o maior tamanho de fonte (entre min_size e start_size) cujo texto
    quebrado em linhas ainda cabe em max_width x max_height. Devolve a fonte,
    as linhas já quebradas, e a altura de linha usada."""
    size = start_size
    while size >= min_size:
        font = _load_font("Fraunces-Variable", size, variation=b"Bold")
        lines = _wrap_to_width(draw, text, font, max_width)
        line_h = round(size * 1.22)
        block_h = line_h * len(lines)
        if block_h <= max_height:
            return font, lines, line_h
        size -= 4
    # não coube nem no tamanho mínimo: usa o mínimo mesmo e deixa estourar
    # um pouco (melhor um card cheio do que um card vazio por erro)
    font = _load_font("Fraunces-Variable", min_size, variation=b"Bold")
    lines = _wrap_to_width(draw, text, font, max_width)
    return font, lines, round(min_size * 1.22)


def render_card(
    text: str,
    index: int,
    total: int,
    category: str,
    background_path: Path | None,
    site_handle: str = "brgrid.com.br",
) -> Image.Image:
    """Renderiza UM card (1080x1920). `index`/`total` são 1-based, usados só
    pra desenhar os tracinhos de progresso no topo (estilo Stories)."""
    color_hex = CATEGORY_COLORS.get(category, DEFAULT_COLOR)

    if background_path and background_path.exists():
        try:
            bg = Image.open(background_path).convert("RGB")
            canvas = _cover_fit(bg, CARD_W, CARD_H)
        except Exception:  # noqa: BLE001 — imagem corrompida/ilegível: cai pro placeholder
            canvas = _diagonal_placeholder(color_hex)
    else:
        canvas = _diagonal_placeholder(color_hex)

    canvas = canvas.convert("RGBA")
    canvas.alpha_composite(_gradient_overlay())
    draw = ImageDraw.Draw(canvas)

    margin = 72

    # tracinhos de progresso (estilo Stories) no topo
    if total > 1:
        gap = 10
        bar_w = (CARD_W - 2 * margin - gap * (total - 1)) / total
        bar_y = 40
        for i in range(total):
            x0 = margin + i * (bar_w + gap)
            fill = (255, 255, 255, 235) if i < index else (255, 255, 255, 80)
            draw.rounded_rectangle(
                [x0, bar_y, x0 + bar_w, bar_y + 6], radius=3, fill=fill
            )
        brand_y = 74
    else:
        brand_y = 46

    # marca BRGrid (canto superior esquerdo) — barrinha inclinada + nome
    mark_font = _load_font("ArchivoBlack-Regular", 34)
    draw.polygon(
        [
            (margin, brand_y + 30),
            (margin + 10, brand_y),
            (margin + 18, brand_y),
            (margin + 8, brand_y + 30),
        ],
        fill=_hex_to_rgb(color_hex),
    )
    draw.text((margin + 26, brand_y - 2), "BRGrid", font=mark_font, fill="white")

    # chip de categoria (canto superior direito)
    chip_font = _load_font("BarlowCondensed-Bold", 30)
    chip_pad_x, chip_pad_y = 20, 10
    chip_w = draw.textlength(category.upper(), font=chip_font) + chip_pad_x * 2
    chip_h = 40 + chip_pad_y
    chip_x1 = CARD_W - margin
    chip_x0 = chip_x1 - chip_w
    draw.rounded_rectangle(
        [chip_x0, brand_y - 6, chip_x1, brand_y - 6 + chip_h],
        radius=8,
        fill=_hex_to_rgb(color_hex),
    )
    draw.text(
        (chip_x0 + chip_pad_x, brand_y - 6 + chip_pad_y / 2 - 1),
        category.upper(),
        font=chip_font,
        fill="white",
    )

    # texto principal do card, ancorado no terço inferior
    footer_zone = 150
    text_max_w = CARD_W - margin * 2
    text_max_h = CARD_H - margin - footer_zone - 640  # limite pra não invadir a área de cima
    text_max_h = max(text_max_h, 420)
    font, lines, line_h = _fit_text_block(draw, text, text_max_w, text_max_h)
    block_h = line_h * len(lines)
    text_y0 = CARD_H - footer_zone - block_h - 24
    title_color = _hex_to_rgb(DEFAULT_COLOR)
    y = text_y0
    for line in lines:
        draw.text((margin, y), line, font=font, fill=title_color)
        y += line_h

    # rodapé: handle do site (ou CTA, decidido por quem chama via `site_handle`)
    foot_font = _load_font("BarlowCondensed-SemiBold", 30)
    draw.text(
        (margin, CARD_H - footer_zone + 24),
        site_handle,
        font=foot_font,
        fill=(255, 255, 255, 220),
    )

    return canvas.convert("RGB")


def generate_card_images(
    texts: list[str],
    category: str,
    background_path: Path | list[Path] | None,
    out_dir: Path,
    site_handle: str = "brgrid.com.br",
    cta_text: str = "Matéria completa em brgrid.com.br",
) -> list[Path]:
    """Renderiza um card por texto em `texts` e salva em out_dir/1.png,
    2.png, ... Último card usa `cta_text` no rodapé em vez do handle
    simples. `background_path` aceita um Path único (repetido em todos os
    cards, comportamento de sempre) OU uma lista de Paths (uma por card,
    alternando em ordem -- volta pro início se houver mais cards que
    imagens; ver MANUAL_IMAGES/build_manual_image_info() em generate.py).
    Devolve a lista de paths salvos (mesma ordem de `texts`)."""
    if isinstance(background_path, list):
        backgrounds = background_path
    elif background_path is not None:
        backgrounds = [background_path]
    else:
        backgrounds = []

    out_dir.mkdir(parents=True, exist_ok=True)
    total = len(texts)
    paths = []
    for i, text in enumerate(texts, start=1):
        handle = cta_text if i == total else site_handle
        bg = backgrounds[(i - 1) % len(backgrounds)] if backgrounds else None
        img = render_card(text, i, total, category, bg, site_handle=handle)
        path = out_dir / f"{i}.png"
        img.save(path, "PNG", optimize=True)
        paths.append(path)
    return paths
