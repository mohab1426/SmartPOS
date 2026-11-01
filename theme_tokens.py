"""SmartPOS design tokens - the single source of truth for colours, type, spacing and shape.

Palette source (supplied reference image "Colors / Palette #1"):

    69D2E7  Turquoise Blue   -> brand / primary
    A7DBD8  Aqua Island      -> secondary
    E0E4CC  Kidnapper        -> neutral surface family (borders, backgrounds)
    F38630  Jaffa            -> warm accent (secondary accent)
    FA6900  Blaze Orange     -> call-to-action accent

The five source colours are kept verbatim in ``SOURCE_PALETTE``.  Everything else is a *derived*
variant (darker for text/links, lighter tints for backgrounds, semantic status colours) chosen so
important text meets WCAG AA contrast (checked by ``tests/test_theme_contrast.py``).

No other module should contain raw hexadecimal colours: use ``T.<TOKEN>`` for Python painting code
or ``qss("... @token@ ...")`` for style-sheet fragments.
"""
from __future__ import annotations

import re

# --------------------------------------------------------------------------- source palette
SOURCE_PALETTE = {
    "turquoise_blue": "#69D2E7",
    "aqua_island": "#A7DBD8",
    "kidnapper": "#E0E4CC",
    "jaffa": "#F38630",
    "blaze_orange": "#FA6900",
}

# --------------------------------------------------------------------------- colour tokens
COLORS: dict[str, str] = {
    # brand
    "primary": "#69D2E7",            # Turquoise Blue (source)
    "primary_strong": "#0C778D",     # darkened Turquoise: links, selected states, focus (AA on white)
    "primary_deep": "#0F4350",       # darkest teal: sidebar surface
    "primary_deep_hover": "#17596B",
    "primary_tint": "#E3F6FA",       # very light Turquoise tint
    "secondary": "#A7DBD8",          # Aqua Island (source)
    "secondary_tint": "#E2F2F0",
    "accent": "#FA6900",             # Blaze Orange (source): the CTA colour
    "accent_hover": "#E85F00",
    "accent_pressed": "#DE5E00",
    "accent_alt": "#F38630",         # Jaffa (source)
    "accent_tint": "#FFEBD9",
    # surfaces
    "bg": "#F4F6EA",                 # window background (Kidnapper lightened)
    "surface": "#FFFFFF",            # cards, inputs, tables
    "surface_alt": "#F9FAF3",        # alternate rows, sections
    "surface_sunken": "#EAEDD9",     # headers, chips, hover
    "elevated": "#FFFFFF",           # dialogs / popups (shadow separates them)
    "kidnapper": "#E0E4CC",          # Kidnapper (source)
    # text
    "text": "#16343C",
    "text_secondary": "#3F5A63",
    "text_muted": "#566E76",
    "text_disabled": "#8A9BA0",
    "text_on_primary_strong": "#FFFFFF",
    "text_on_accent": "#1E1208",     # dark ink: white on Blaze Orange fails AA
    "text_on_deep": "#FFFFFF",
    "text_on_deep_muted": "#BFE3E8",
    "text_on_primary": "#0B2E36",    # ink on Turquoise Blue
    # lines
    "border": "#D3D9B9",
    "border_soft": "#E3E7D2",
    "border_strong": "#AEB78F",
    "sidebar_border": "#1F5A6B",
    # semantic
    "success": "#17714F",
    "success_bg": "#E1F3EA",
    "success_border": "#B5DFC9",
    "warning": "#8A4A0A",
    "warning_bg": "#FFEBD6",
    "warning_border": "#F6C795",
    "error": "#B3322B",
    "error_hover": "#972821",
    "error_bg": "#FBE7E5",
    "error_border": "#F1BDB8",
    "info": "#0B6A83",
    "info_bg": "#DEF3F8",
    "info_border": "#B2E0EB",
    # focus ring
    "focus": "#0C778D",
}

# Series colours for painted charts (order = priority).
CHART_SERIES = ["#0C778D", "#FA6900", "#17714F", "#F38630", "#0B6A83", "#0F4350"]

TYPOGRAPHY = {
    "font_family": '"Segoe UI", "Noto Sans Arabic", "Tahoma", "Arial"',
    "size_xs": 11, "size_sm": 12, "size_md": 13, "size_lg": 16, "size_xl": 20, "size_title": 24,
    "weight_regular": 400, "weight_medium": 600, "weight_bold": 700, "weight_heavy": 800,
}
SPACING = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24}
RADII = {"sm": 6, "md": 9, "lg": 12, "xl": 16, "pill": 16}
# Qt style sheets have no box-shadow; "shadow" tokens are used with QGraphicsDropShadowEffect.
SHADOWS = {"card": (0, 2, 12, 28), "dialog": (0, 8, 28, 60)}   # dx, dy, blur, alpha(0-255)

_flat = {**COLORS}
globals().update({k.upper(): v for k, v in _flat.items()})  # T.PRIMARY, T.TEXT, ... for painting code

_TOKEN = re.compile(r"@([a-z0-9_]+)@")


def qss(template: str) -> str:
    """Replace ``@token@`` placeholders with token values (colours, then spacing/radii/type)."""
    lookup = {**COLORS,
              **{f"r_{k}": str(v) for k, v in RADII.items()},
              **{f"s_{k}": str(v) for k, v in SPACING.items()},
              **{f"f_{k[5:]}": str(v) for k, v in TYPOGRAPHY.items() if k.startswith("size_")},
              "font_family": TYPOGRAPHY["font_family"]}

    def sub(match: re.Match) -> str:
        key = match.group(1)
        if key not in lookup:
            raise KeyError(f"Unknown theme token: {key}")
        return lookup[key]
    return _TOKEN.sub(sub, template)


# --------------------------------------------------------------------------- WCAG helpers
def _linear(channel: int) -> float:
    c = channel / 255
    return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4


def relative_luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _linear(r) + 0.7152 * _linear(g) + 0.0722 * _linear(b)


def contrast_ratio(foreground: str, background: str) -> float:
    a, b = relative_luminance(foreground), relative_luminance(background)
    hi, lo = max(a, b), min(a, b)
    return (hi + 0.05) / (lo + 0.05)
