# SmartPOS Design System

Source of truth: `theme_tokens.py`. Stylesheet: `ui_theme.py` (generated from tokens via `qss()`); no raw hex outside `theme_tokens.py` (enforced by `tests/test_theme_contrast.py`).

## Palette extracted from the supplied image (verbatim)
| Token | Hex | Name | Role |
|---|---|---|---|
| primary | #69D2E7 | Turquoise Blue | brand, selected nav / category, focus tint |
| secondary | #A7DBD8 | Aqua Island | hover tints |
| kidnapper | #E0E4CC | Kidnapper | neutral family (window bg is a lightened mix) |
| accent_alt | #F38630 | Jaffa | warning button |
| accent | #FA6900 | Blaze Orange | call-to-action (Primary buttons, checkout) |

## Derived tokens (not in the image; chosen to keep the identity and meet contrast)
primary_strong #0C778D (links, focus ring, selected checkbox), primary_deep #0F4350 (sidebar), bg #F4F6EA, surface #FFFFFF, surface_alt #F9FAF3, surface_sunken #EAEDD9, text #16343C, text_secondary #3F5A63, text_muted #566E76, borders #D3D9B9 / #E3E7D2 / #AEB78F, success #17714F, warning #8A4A0A, error #B3322B, info #0B6A83 (+ tinted backgrounds). Full list in `COLORS`.

Important: white text on Blaze Orange is ~2.9:1 and fails WCAG AA, so Primary buttons use dark ink `#1E1208` (6.2:1). Tested pairs: 36 text/background combinations, all >= 4.5:1. Not covered: disabled text (exempt), and any pair not listed in the test.

## Other tokens
Typography, spacing, radii and shadow tuples are in `theme_tokens.py`. Shadows are defined but NOT yet applied (Qt style sheets have no box-shadow; no QGraphicsDropShadowEffect was added).

## Before / after
- Before: blue/navy theme (#2457A6 on #F4F7FB, sidebar #111C2E), ~120 hex values in `ui_theme.py` and ~30 inline in `app.py`.
- After: teal sidebar with turquoise selected item and orange accent, kidnapper-tinted background, token-driven stylesheet, inline styles routed through `qss()` / `T.*`, header labels transparent, sidebar spacing tightened so brand and all ten nav items fit at 1280x780 (the brand was already clipped in the original build).
- Charts and dashboard cards use token colours.

## Not done
Screen-by-screen visual review of Products, Invoices, Purchases, Suppliers, Reports, Users, Customers, Shifts and dialogs (only the cashier screen at 1280x780 and the login/first-run dialogs were inspected by eye). Spin-box arrows render oddly in the offscreen platform (also in the original build) - not investigated. Real Windows font rendering is unverified; offscreen used DejaVu.
