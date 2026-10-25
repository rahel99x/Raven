"""A shared terminal palette, with true colour and deliberate portable fallbacks.

Styles remain compact ``+``-joined strings. In addition to the traditional names,
``fg:#rrggbb`` and ``bg:#rrggbb`` let charts colour individual character cells.
The module never emits cursor controls or imports curses.
"""
from __future__ import annotations

import colorsys
import math
import os
from dataclasses import dataclass
from functools import lru_cache
from typing import Optional

RGB = tuple[int, int, int]

# Light ink on the user's terminal background. Semantic names let views describe
# meaning rather than bind themselves to one terminal's ANSI colour definitions.
PALETTE = {
    "green": "#6ee7b7", "yellow": "#fbbf24", "red": "#fb7185",
    "cyan": "#67e8f9", "magenta": "#c4b5fd", "blue": "#93c5fd",
    "white": "#e2e8f0", "black": "#0b1020", "muted": "#94a3b8",
    "faint": "#475569", "surface": "#172033", "border": "#334155",
    "canvas": "#0b1020", "surface-raised": "#202c42", "surface-sunken": "#101827",
    "border-strong": "#64748b", "text-secondary": "#b8c7da", "track": "#24445b",
    "chart-1": "#67e8f9", "chart-2": "#c4b5fd", "chart-3": "#6ee7b7",
    "chart-4": "#fbbf24", "chart-5": "#fb7185", "chart-6": "#93c5fd",
    "selection": "#1e3a5f", "orange": "#fb923c", "cursor": "#67e8f9",
    "notice": "#92400e", "notice-text": "#fff7ed",
}
LIGHT_PALETTE = {
    "green": "#166534", "yellow": "#854d0e", "red": "#be123c",
    "cyan": "#0e7490", "magenta": "#6d28d9", "blue": "#1d4ed8",
    "white": "#172033", "black": "#ffffff", "muted": "#475569",
    "faint": "#64748b", "surface": "#f1f5f9", "border": "#94a3b8",
    "canvas": "#ffffff", "surface-raised": "#e2e8f0", "surface-sunken": "#f8fafc",
    "border-strong": "#64748b", "text-secondary": "#334155", "track": "#cbd5e1",
    "chart-1": "#0e7490", "chart-2": "#6d28d9", "chart-3": "#166534",
    "chart-4": "#854d0e", "chart-5": "#be123c", "chart-6": "#1d4ed8",
    "selection": "#dbeafe", "orange": "#9a3412", "cursor": "#0e7490",
    "notice": "#ffedd5", "notice-text": "#7c2d12",
}
# Named editor themes retain their familiar canvas and accent hues. Every token
# has a value: new panels and old semantic styles use the same active palette.
DARCULA_PALETTE = dict(PALETTE, **{
    "canvas": "#2b2b2b", "black": "#2b2b2b", "surface": "#313335",
    "surface-raised": "#3c3f41", "surface-sunken": "#232525",
    "white": "#a9b7c6", "text-secondary": "#c4ccd4", "muted": "#9b9fa7",
    "faint": "#737a84", "border": "#555b64", "border-strong": "#8c929c",
    "green": "#a9c58f", "yellow": "#ffc66d", "red": "#ff6b68",
    "cyan": "#56c4d8", "magenta": "#c792ea", "blue": "#6ea5ff",
    "track": "#42505a", "selection": "#214283", "orange": "#ffad66",
    "cursor": "#56c4d8", "notice": "#614321", "notice-text": "#ffe0b2",
    "chart-1": "#56c4d8", "chart-2": "#c792ea", "chart-3": "#a9c58f",
    "chart-4": "#ffc66d", "chart-5": "#ff6b68", "chart-6": "#6ea5ff",
})
MODNOKAI_PALETTE = dict(PALETTE, **{
    "canvas": "#272822", "black": "#272822", "surface": "#30312b",
    "surface-raised": "#3e3d32", "surface-sunken": "#20211c",
    "white": "#f8f8f2", "text-secondary": "#dfdfd4", "muted": "#b0ad96",
    "faint": "#817f71", "border": "#5c5a4e", "border-strong": "#a09c88",
    "green": "#a6e22e", "yellow": "#e6db74", "red": "#ff4d83",
    "cyan": "#66d9ef", "magenta": "#ae81ff", "blue": "#78a8e8",
    "track": "#49483e", "selection": "#49483e", "orange": "#fd971f",
    "cursor": "#66d9ef", "notice": "#66401c", "notice-text": "#ffe0b2",
    "chart-1": "#66d9ef", "chart-2": "#ae81ff", "chart-3": "#a6e22e",
    "chart-4": "#e6db74", "chart-5": "#ff4d83", "chart-6": "#78a8e8",
})
GRUVBOX_DARK_PALETTE = dict(PALETTE, **{
    "canvas": "#282828", "black": "#282828", "surface": "#32302f",
    "surface-raised": "#3c3836", "surface-sunken": "#1d2021",
    "white": "#ebdbb2", "text-secondary": "#d5c4a1", "muted": "#bdae93",
    "faint": "#928374", "border": "#665c54", "border-strong": "#a89984",
    "green": "#b8bb26", "yellow": "#fabd2f", "red": "#ff5c49",
    "cyan": "#8ec07c", "magenta": "#d3869b", "blue": "#83a598",
    "track": "#504945", "selection": "#504945", "orange": "#fe8019",
    "cursor": "#8ec07c", "notice": "#68441d", "notice-text": "#f9e1b2",
    "chart-1": "#8ec07c", "chart-2": "#d3869b", "chart-3": "#b8bb26",
    "chart-4": "#fabd2f", "chart-5": "#ff5c49", "chart-6": "#83a598",
})
CATPPUCCIN_MOCHA_PALETTE = dict(PALETTE, **{
    "canvas": "#1e1e2e", "black": "#1e1e2e", "surface": "#242436",
    "surface-raised": "#313244", "surface-sunken": "#181825",
    "white": "#cdd6f4", "text-secondary": "#bac2de", "muted": "#a6adc8",
    "faint": "#7f849c", "border": "#585b70", "border-strong": "#9399b2",
    "green": "#a6e3a1", "yellow": "#f9e2af", "red": "#f38ba8",
    "cyan": "#89dceb", "magenta": "#cba6f7", "blue": "#89b4fa",
    "track": "#45475a", "selection": "#45475a", "orange": "#fab387",
    "cursor": "#89dceb", "notice": "#5c473d", "notice-text": "#f9e2af",
    "chart-1": "#89dceb", "chart-2": "#cba6f7", "chart-3": "#a6e3a1",
    "chart-4": "#f9e2af", "chart-5": "#f38ba8", "chart-6": "#89b4fa",
})
NORD_PALETTE = dict(PALETTE, **{
    "canvas": "#2e3440", "black": "#2e3440", "surface": "#343b49",
    "surface-raised": "#3b4252", "surface-sunken": "#272c36",
    "white": "#eceff4", "text-secondary": "#e5e9f0", "muted": "#c0c8d5",
    "faint": "#8894a8", "border": "#65738b", "border-strong": "#a8b4c8",
    "green": "#a3be8c", "yellow": "#ebcb8b", "red": "#e58c95",
    "cyan": "#8fbcbb", "magenta": "#b991b2", "blue": "#81a1c1",
    "track": "#4c566a", "selection": "#4c566a", "orange": "#dba484",
    "cursor": "#8fbcbb", "notice": "#614d3e", "notice-text": "#ebcb8b",
    "chart-1": "#8fbcbb", "chart-2": "#b991b2", "chart-3": "#a3be8c",
    "chart-4": "#ebcb8b", "chart-5": "#e58c95", "chart-6": "#81a1c1",
})
TOKYO_NIGHT_PALETTE = dict(PALETTE, **{
    "canvas": "#1a1b26", "black": "#1a1b26", "surface": "#202231",
    "surface-raised": "#292e42", "surface-sunken": "#16161e",
    "white": "#c0caf5", "text-secondary": "#a9b1d6", "muted": "#949fc9",
    "faint": "#737da5", "border": "#4f597e", "border-strong": "#909bc4",
    "green": "#9ece6a", "yellow": "#e0af68", "red": "#f7768e",
    "cyan": "#7dcfff", "magenta": "#bb9af7", "blue": "#7aa2f7",
    "track": "#394b70", "selection": "#283457", "orange": "#ff9e64",
    "cursor": "#7dcfff", "notice": "#64432d", "notice-text": "#ffce9e",
    "chart-1": "#7dcfff", "chart-2": "#bb9af7", "chart-3": "#9ece6a",
    "chart-4": "#e0af68", "chart-5": "#f7768e", "chart-6": "#7aa2f7",
})
ROSE_PINE_DAWN_PALETTE = dict(LIGHT_PALETTE, **{
    "canvas": "#faf4ed", "black": "#faf4ed", "surface": "#fffaf3",
    "surface-raised": "#f2e9e1", "surface-sunken": "#f2e9de",
    "white": "#575279", "text-secondary": "#625c7e", "muted": "#746c87",
    "faint": "#82788f", "border": "#b5a9b4", "border-strong": "#8b7d92",
    "green": "#426b65", "yellow": "#865f1d", "red": "#b43859",
    "cyan": "#286983", "magenta": "#82559a", "blue": "#465e9b",
    "track": "#d9cbd3", "selection": "#e4d8e3", "orange": "#9d542b",
    "cursor": "#286983", "notice": "#f5dfc5", "notice-text": "#754622",
    "chart-1": "#286983", "chart-2": "#82559a", "chart-3": "#426b65",
    "chart-4": "#865f1d", "chart-5": "#b43859", "chart-6": "#465e9b",
})
# Public offerings are intentionally distinct from compatibility modes. Old
# configs retain reader/monochrome/colour-blind support without cluttering the
# theme menu or the cycle shortcut. Retired colour schemes migrate on read.
THEME_NAMES = ("gruvbox-dark", "modnokai", "darcula", "catppuccin-mocha", "nord",
               "tokyo-night", "rose-pine-dawn", "system")
LEGACY_THEME_NAMES = ("mono", "high", "cb", "reader")
ACCEPTED_THEME_NAMES = THEME_NAMES + LEGACY_THEME_NAMES
THEME_LABELS = {"gruvbox-dark": "Gruvbox", "modnokai": "Modnokai", "darcula": "Darcula",
                "catppuccin-mocha": "Catppuccin Mocha", "nord": "Nord",
                "tokyo-night": "Tokyo Night", "rose-pine-dawn": "Rosé Pine Dawn", "system": "System"}
THEME_ALIASES = {"monokai": "modnokai", "gruvbox": "gruvbox-dark",
                 "default": "gruvbox-dark", "dark": "gruvbox-dark", "light": "rose-pine-dawn",
                 "terminal": "system", "rose-pine": "rose-pine-dawn", "rosé-pine-dawn": "rose-pine-dawn",
                 "catppuccin": "catppuccin-mocha", "tokyonight": "tokyo-night"}
_THEME_PALETTES = {"darcula": DARCULA_PALETTE, "modnokai": MODNOKAI_PALETTE,
                   "gruvbox-dark": GRUVBOX_DARK_PALETTE, "catppuccin-mocha": CATPPUCCIN_MOCHA_PALETTE,
                   "nord": NORD_PALETTE, "tokyo-night": TOKYO_NIGHT_PALETTE,
                   "rose-pine-dawn": ROSE_PINE_DAWN_PALETTE}
# Compatibility for fixed styles in older views. Only known application colours
# are translated; an unrelated custom RGB value retains its exact meaning.
_LEGACY_COLORS = {value: key for key, value in reversed(tuple(PALETTE.items()))}
_LEGACY_COLORS.update({"#22d3ee": "cyan", "#a78bfa": "magenta", "#34d399": "green",
                       "#ec4899": "magenta", "#155e75": "track", "#164e63": "track",
                       "#38bdf8": "blue", "#3b82f6": "blue"})
ALIASES = {
    "accent": "cyan", "success": "green", "warning": "yellow", "warn": "yellow",
    "danger": "red", "error": "red", "text": "white", "info": "blue",
    "cool": "blue", "hot": "red", "heading": "accent", "secondary": "text-secondary",
    # Pointer and slider glyphs use the same active accent as other controls.
    # This alias also covers cached fg:/bg: styles and future theme palettes.
    "cursor": "accent",
    # A semantic fill marker lets selectors cross decorative range interiors.
    # Range borders and fitted/observed strokes remain protected foreground ink.
    "chart-fill": "chart-1",
}
CB_MAP = {"green": "blue", "red": "yellow", "yellow": "magenta"}
FLAGS = {"bold": "1", "dim": "2", "rev": "7", "under": "4"}

# Indexed ANSI colours, used both by ANSI output and curses quantisation.
BASIC: tuple[RGB, ...] = ((0, 0, 0), (205, 0, 0), (0, 205, 0), (205, 205, 0),
                          (0, 0, 238), (205, 0, 205), (0, 205, 205), (229, 229, 229))
BRIGHT: tuple[RGB, ...] = ((127, 127, 127), (255, 0, 0), (0, 255, 0), (255, 255, 0),
                           (92, 92, 255), (255, 0, 255), (0, 255, 255), (255, 255, 255))
_LEVELS = (0, 95, 135, 175, 215, 255)
INDEXED: tuple[RGB, ...] = BASIC + BRIGHT + tuple((r, g, b) for r in _LEVELS for g in _LEVELS for b in _LEVELS) + tuple((v, v, v) for v in range(8, 239, 10))
_BASIC_SEMANTIC = {tuple(int(palette[role][i:i + 2], 16) for i in (1, 3, 5)): index
                   for palette in (PALETTE, *_THEME_PALETTES.values())
                   for role, index in (("red", 1), ("green", 2), ("yellow", 3),
                                       ("blue", 4), ("magenta", 5), ("cyan", 6), ("orange", 3))}


def rgb(value: str) -> RGB:
    """Parse exactly a six-digit RGB colour, rejecting terminal control text."""
    if (not isinstance(value, str) or len(value) != 7 or not value.startswith("#") or
            any(ch not in "0123456789abcdefABCDEF" for ch in value[1:])):
        raise ValueError("colour must be #rrggbb")
    return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))  # type: ignore[return-value]


def canonical_theme(theme: str) -> str:
    """Normalize human-friendly theme names; unknown names remain rejectable."""
    if not isinstance(theme, str):
        return ""
    name = "-".join(theme.strip().lower().replace("_", "-").split())
    return THEME_ALIASES.get(name, name)


def _tokens(theme: str) -> dict[str, str]:
    return _THEME_PALETTES.get(canonical_theme(theme), PALETTE)


@lru_cache(maxsize=4096)
def cell_style(style: str = "", theme: str = "default", surface: str = "canvas") -> str:
    """Inherit the complete canvas without overriding explicit cell colours.

    Terminal, monochrome and reader modes retain the emulator's default canvas.
    Styled coloured output paints whitespace too, including short and empty rows.
    """
    theme = canonical_theme(theme)
    if theme in ("system", "mono", "reader"):
        return style
    return "+".join(part for part in ("text", "bg:" + surface, style) if part)


def gradient_style(start: str, end: str, fraction: float) -> str:
    """A theme-aware gradient expression that remains valid in cached rows."""
    t = max(0.0, min(1.0, fraction)) if math.isfinite(fraction) else 0.0
    return f"gradient:{start}:{end}:{t:.6f}"


def _color(value: str, theme: str, tokens: dict[str, str]) -> RGB:
    value = _alias(value)
    if theme not in ("default", "dark", "terminal", "mono", "reader"):
        value = _LEGACY_COLORS.get(value.lower(), value)
    return rgb(tokens.get(value, value))


def gradient(start: str, end: str, fraction: float) -> str:
    """Interpolate a bounded RGB gradient for foreground and background cells."""
    a, b = rgb(start), rgb(end)
    t = max(0.0, min(1.0, fraction))
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(a, b))


def colors_disabled() -> bool:
    return bool(os.environ.get("NO_COLOR")) or os.environ.get("TERM", "").lower() == "dumb"


def detect_color_depth() -> int:
    """Return 24, 256, 8 or 0 without probing the terminal or running a command."""
    if colors_disabled():
        return 0
    term = os.environ.get("TERM", "").lower()
    if os.environ.get("COLORTERM", "").lower() in ("truecolor", "24bit") or "direct" in term:
        return 24
    return 256 if "256color" in term else 8


color_depth = detect_color_depth


@lru_cache(maxsize=4096)
def color_index(value: RGB, count: int = 256, background: bool = False) -> int:
    """Quantise colours, keeping semantic hues in the eight-colour fallback."""
    if count < 256:
        if not background and value in _BASIC_SEMANTIC:
            return _BASIC_SEMANTIC[value]
        # Eight-colour backgrounds need a stable dark or light canvas. A dark
        # navy RGB must not become the terminal's saturated blue background.
        if background and max(value) < 128:
            return 0
        if background and min(value) > 180:
            return 7
        hue, light, saturation = colorsys.rgb_to_hls(*(v / 255 for v in value))
        if saturation <= .25:
            return 0 if light < .3 else 7
        hue *= 360
        return 1 if hue < 30 or hue >= 330 else (3 if hue < 90 else (2 if hue < 165 else (6 if hue < 205 else (4 if hue < 265 else 5))))
    return min(range(len(INDEXED)), key=lambda i: sum((a - b) ** 2 for a, b in zip(value, INDEXED[i])))


def _cb_rgb(value: RGB) -> RGB:
    """Keep luminance structure while moving ambiguous green/red chart gradients."""
    h, light, sat = colorsys.rgb_to_hls(*(v / 255 for v in value))
    if sat < .2:
        return value
    degrees = h * 360
    target = 215 if 75 <= degrees <= 165 else (40 if degrees <= 25 or degrees >= 335 else (275 if 25 < degrees < 75 else degrees))
    return tuple(round(v * 255) for v in colorsys.hls_to_rgb(target / 360, light, sat))  # type: ignore[return-value]


@dataclass(frozen=True)
class Style:
    flags: tuple[str, ...] = ()
    foreground: Optional[RGB] = None
    background: Optional[RGB] = None
    # System colours are terminal palette slots, not guessed RGB values.
    foreground_index: Optional[int] = None
    background_index: Optional[int] = None


def theme_tokens(theme: str = "default") -> dict[str, str]:
    """A fresh reference map; System's actual terminal RGB values are unknown.

    The System painter resolves these historical application colours to native
    palette slots. This map describes no observed terminal or desktop settings.
    """
    tokens = dict(_tokens(theme))
    tokens["cursor"] = tokens[_alias("cursor")]
    return tokens


def chart_colors(theme: str = "default") -> tuple[str, ...]:
    """Ordered, contrasting chart colours shared by legends and plots."""
    tokens = theme_tokens(theme)
    return tuple(tokens[f"chart-{n}"] for n in range(1, 7))


def _alias(token: str) -> str:
    for _ in range(3):
        replacement = ALIASES.get(token, token)
        if replacement == token:
            break
        token = replacement
    return token


_SYSTEM_INDEX = {"black": 0, "red": 1, "green": 2, "yellow": 3, "blue": 4,
                 "magenta": 5, "cyan": 6, "orange": 3, "notice": 3,
                 "chart-1": 6, "chart-2": 5, "chart-3": 2, "chart-4": 3,
                 "chart-5": 1, "chart-6": 4, "selection": 4}
_SYSTEM_DEFAULTS = frozenset(("white", "text-secondary", "muted", "faint", "canvas",
                              "surface", "surface-raised", "surface-sunken", "border",
                              "border-strong", "track", "notice-text"))


def _system_style(style: str) -> Style:
    """Use native ANSI slots and default paper, without querying the terminal.

    Exact colours from an embedded terminal or an explicit custom RGB stay
    exact. Raven's semantic colours and historical fixed application colours
    use the user's terminal palette. Gradient charts use its eight native hues.
    """
    flags: list[str] = []
    foreground = background = None
    foreground_index = background_index = None
    terminal_colors = "terminal" in style.split("+")
    for token in style.split("+"):
        if token == "terminal":
            continue
        if token in FLAGS:
            flags.append(token)
            continue
        if token == "sel":
            flags.extend(("bold", "rev"))
            continue
        if token.startswith("bg-raw:"):
            try:
                background = rgb(token[7:])
                background_index = None
            except ValueError:
                pass
            continue
        if token.startswith("gradient:"):
            try:
                _, start, end, fraction = token.split(":")
                amount = float(fraction)
                if not math.isfinite(amount):
                    continue
                a, b = _color(start, "system", PALETTE), _color(end, "system", PALETTE)
                amount = max(0.0, min(1.0, amount))
                value_rgb = tuple(round(x + (y - x) * amount) for x, y in zip(a, b))
                foreground_index = color_index(value_rgb, 8)
                foreground = None
            except (TypeError, ValueError):
                pass
            continue
        is_background = token.startswith("bg:")
        value = token[3:] if token.startswith(("fg:", "bg:")) else token
        value = _alias(value)
        if not terminal_colors:
            if is_background and value.lower() == PALETTE["canvas"]:
                value = "canvas"
            value = _alias(_LEGACY_COLORS.get(value.lower(), value))
        color = index = None
        if value in _SYSTEM_DEFAULTS:
            if not is_background and value in ("muted", "faint", "border", "track"):
                flags.append("dim")
        elif value in _SYSTEM_INDEX:
            index = _SYSTEM_INDEX[value]
            if not is_background and value in ("red", "yellow", "orange"):
                flags.append("bold")
        else:
            try:
                color = rgb(value)
            except ValueError:
                continue
        if is_background:
            background, background_index = color, index
        else:
            foreground, foreground_index = color, index
    return Style(tuple(dict.fromkeys(flags)), foreground, background,
                 foreground_index, background_index)


@lru_cache(maxsize=4096)
def resolve(style: str, theme: str = "default") -> Style:
    """Resolve semantic, custom and accessibility styles once for both painters."""
    theme = canonical_theme(theme)
    if theme == "system":
        return _system_style(style)
    flags: list[str] = []
    foreground = background = None
    plain = theme in ("mono", "reader")
    tokens = _tokens(theme)
    colored = False
    # Child terminal applications own their explicit SGR colours. A matching
    # legacy Raven RGB must not accidentally become a semantic theme colour.
    # Defaults still inherit the pane theme; monochrome/reader remain plain.
    terminal_colors = "terminal" in style.split("+")
    for token in style.split("+"):
        if token == "terminal":
            continue
        if token in FLAGS:
            flags.append(token)
            continue
        if token == "sel":
            flags.append("bold")
            if plain or theme == "terminal":
                flags.append("rev")
            else:
                foreground = rgb(tokens["white"])
                background = rgb(tokens["selection"])
                colored = True
            continue
        token = _alias(token)
        if plain:
            if token in ("red", "yellow", "magenta"):
                flags.append("bold")
            continue
        if token.startswith("bg-raw:"):
            # Cosmetic overlays inherit an already resolved painted RGB. Do
            # not recolour it a second time through theme aliases or CB maps.
            # This internal token remains a strictly validated six-digit RGB.
            try:
                background = rgb(token[7:])
                colored = True
            except ValueError:
                pass
            continue
        if token.startswith("gradient:"):
            try:
                _, start, end, fraction = token.split(":")
                amount = float(fraction)
                if not math.isfinite(amount):
                    continue
                a, b = _color(start, theme, tokens), _color(end, theme, tokens)
                amount = max(0.0, min(1.0, amount))
                value_rgb = tuple(round(x + (y - x) * amount) for x, y in zip(a, b))
                foreground = _cb_rgb(value_rgb) if theme == "cb" else value_rgb
                colored = True
            except (TypeError, ValueError):
                continue
            continue
        if theme == "terminal" and token in ("white", "text-secondary", "muted", "faint"):
            foreground = None
            if token in ("muted", "faint"):
                flags.append("dim")
            continue
        background_token = token.startswith("bg:")
        if token.startswith(("fg:", "bg:")):
            value = token[3:]
            try:
                if theme == "terminal" and background_token and value in ("canvas", "surface", "surface-raised", "surface-sunken"):
                    continue
                value_rgb = rgb(value) if terminal_colors and value.startswith("#") else _color(value, theme, tokens)
            except ValueError:
                continue
            if theme == "cb" and not (terminal_colors and value.startswith("#")):
                value_rgb = _cb_rgb(value_rgb)
        else:
            token = CB_MAP.get(token, token) if theme == "cb" else token
            if token not in tokens:
                continue
            value_rgb = rgb(tokens[token])
        if background_token:
            background = value_rgb
        else:
            foreground = value_rgb
        colored = True
    if theme == "high" and colored:
        flags.append("bold")
    return Style(tuple(dict.fromkeys(flags)), foreground, background)


def ansi_codes(style: str, color_depth: Optional[int] = None, theme: str = "default") -> str:
    """Return SGR codes only; the painter owns escape delimiters and resets.

    A depth of zero disables attributes too. Explicit depths make exported ANSI
    output reproducible; NO_COLOR and TERM=dumb still take precedence.
    """
    depth = detect_color_depth() if color_depth is None else color_depth
    if not depth or colors_disabled():
        return ""
    return _ansi_codes(style, depth, theme)


@lru_cache(maxsize=4096)
def _ansi_codes(style: str, depth: int, theme: str) -> str:
    parsed = resolve(style, theme)
    codes = [FLAGS[flag] for flag in parsed.flags]
    if depth != 24 and depth < 256 and "sel" in style.split("+") and parsed.background is not None:
        canvas = resolve(cell_style("", theme), theme).background
        if canvas is not None and color_index(parsed.background, 8, background=True) == color_index(canvas, 8, background=True):
            if "7" not in codes:
                codes.append("7")
    for value, index, foreground in ((parsed.foreground, parsed.foreground_index, True),
                                     (parsed.background, parsed.background_index, False)):
        if index is not None:
            codes.append(str((30 if foreground else 40) + index))
            continue
        if value is None:
            continue
        if depth == 24:
            codes.extend(("38" if foreground else "48", "2", *(str(v) for v in value)))
        elif depth >= 256:
            codes.extend(("38" if foreground else "48", "5", str(color_index(value))))
        else:
            codes.append(str((30 if foreground else 40) + color_index(value, 8, background=not foreground)))
    return ";".join(codes)
