"""Design-system guards: palette fidelity, WCAG AA contrast, no stray raw colours."""
import re
from pathlib import Path

import pytest

import theme_tokens as T

ROOT = Path(__file__).resolve().parents[1]
C = T.COLORS

# (foreground, background) pairs that are really used for text/controls in the UI.  AA = 4.5:1.
TEXT_PAIRS = [
    ("text", "bg"), ("text", "surface"), ("text", "surface_alt"), ("text", "primary_tint"), ("text", "accent_tint"),
    ("text_secondary", "bg"), ("text_secondary", "surface"), ("text_secondary", "surface_sunken"),
    ("text_muted", "bg"), ("text_muted", "surface"), ("text_muted", "surface_sunken"),
    ("text_on_primary_strong", "primary_strong"),
    ("text_on_accent", "accent"), ("text_on_accent", "accent_hover"), ("text_on_accent", "accent_pressed"),
    ("text_on_accent", "accent_alt"),
    ("text_on_deep", "primary_deep"), ("text_on_deep", "primary_deep_hover"),
    ("text_on_deep_muted", "primary_deep"), ("text_on_deep_muted", "primary_deep_hover"),
    ("text_on_deep", "error"), ("text_on_deep", "error_hover"), ("text_on_deep", "success"),
    ("text_on_primary", "primary"),
    ("success", "success_bg"), ("warning", "warning_bg"), ("error", "error_bg"), ("info", "info_bg"),
    ("success", "surface"), ("warning", "surface"), ("error", "surface"), ("info", "surface"),
    ("primary_strong", "surface"), ("primary_strong", "bg"), ("primary_strong", "primary_tint"),
    ("error", "surface_sunken"), ("warning", "surface_alt"),
]


@pytest.mark.parametrize("fg,bg", TEXT_PAIRS)
def test_text_pairs_meet_wcag_aa(fg, bg):
    ratio = T.contrast_ratio(C[fg], C[bg])
    assert ratio >= 4.5, f"{fg} on {bg} = {ratio:.2f}:1 (< 4.5)"


def test_white_on_blaze_orange_is_NOT_used_because_it_fails_aa():
    # documents the reason primary buttons use dark ink on the accent colour
    assert T.contrast_ratio("#FFFFFF", C["accent"]) < 4.5
    assert T.contrast_ratio(C["text_on_accent"], C["accent"]) >= 4.5


def test_source_palette_is_preserved_verbatim():
    assert T.SOURCE_PALETTE == {"turquoise_blue": "#69D2E7", "aqua_island": "#A7DBD8", "kidnapper": "#E0E4CC",
                                "jaffa": "#F38630", "blaze_orange": "#FA6900"}
    assert (C["primary"], C["secondary"], C["kidnapper"], C["accent_alt"], C["accent"]) == (
        "#69D2E7", "#A7DBD8", "#E0E4CC", "#F38630", "#FA6900")


def test_stylesheet_has_no_unresolved_placeholders():
    import ui_theme
    assert not re.findall(r"@[a-z0-9_]+@", ui_theme.APP_STYLESHEET)
    with pytest.raises(KeyError):
        T.qss("color:@no_such_token@;")


@pytest.mark.parametrize("name", ["app.py", "ui_theme.py", "login_dialog.py"])
def test_no_raw_hex_colours_outside_theme_tokens(name):
    text = (ROOT / name).read_text(encoding="utf-8")
    assert not re.findall(r"#[0-9A-Fa-f]{6}\b", text), f"raw hex colour found in {name}; use theme_tokens"
