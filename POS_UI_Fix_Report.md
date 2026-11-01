# POS UI Fix Report — Cashier invoice panel overlap

## STATUS: code fix delivered, NOT yet verified visually or by a running test

This fix was written in an environment where **PySide6 could not be installed** (no network, no Qt Python
bindings). Therefore:

- The application was **never launched**, so **no "after" screenshots exist** (`POS_1280x780.png` etc. were
  **not** produced) and none of the layout assertions were **executed**.
- What was actually run: `py_compile`/`compileall` on all changed files, and a Qt-free stub build of
  `create_cashier_page()` that proves the new code has no name/attribute/signature errors. That stub run says
  nothing about geometry.
- The defect is therefore **not confirmed fixed**. Run the command in "How to verify" and inspect the
  screenshots before accepting this change. If any check or screenshot fails, send me the
  `layout_results.json` and the PNGs and I will iterate.

## Root cause (derived from the supplied `pos.png` and the source)
`SmartPOS.__init__` calls `setMinimumSize(1000, 650)`. When a top-level widget has an explicit minimum size,
Qt does **not** raise it to the layout's own minimum. The old invoice panel was one `QVBoxLayout` of
fixed-minimum-height controls (two 42 px spin boxes, two 38 px combos, a 48 px checkout button, a 22 px total
label, 10 × 10 px spacing, two rows of 36-42 px buttons...) whose minimum height is about 740 px, while the
panel only receives about 590 px at 1280x780 (about 460 px at the 650 px minimum). The cart table is the only
flexible item and was already at its minimum (header only), so Qt laid the remaining widgets out *below*
their minimum heights, stacking them on top of each other: the second button row overlapped the first and the
"الإجمالي قبل الخصم" label. The same overflow explains the clipped spin-box text and the clipped total label
in the screenshot. Fixing the grid alone would not have helped.

## Modified files
| File | Change |
|---|---|
| `app.py` | `create_cashier_page()` invoice panel rebuilt (cart/discount/payment/footer); product area min width 300 and stretch 3 vs panel stretch 2. No other function touched; no signal changed. |
| `ui_theme.py` | New `Invoice*` styles replacing inline `setStyleSheet` on the panel's labels. |
| `scripts/layout_regression_test.py` | **New** real-window geometry test (5 window sizes, screenshots). |
| `tests/test_gui_layout.py` | **New** pytest wrapper (skips without PySide6). |
| `.github/workflows/ci.yml` | New `gui-layout` job (Ubuntu, offscreen) uploading the screenshots. |
| `docs/POS_UI_LAYOUT.md`, `docs/TESTING.md`, `CHANGELOG.md` | Documentation. |
| `docs/screenshots/POS_before_defect.png` | The supplied defect screenshot (before). |

Not modified: `services/`, `database.py`, migrations, schema, accounting, payment/inventory/auth/ledger/returns
logic, `update_cart_totals`, `checkout`, all existing tests.

## Layout approach
See `docs/POS_UI_LAYOUT.md`. Summary:
- Panel = `QVBoxLayout`: a **vertical `QScrollArea`** (header row, cart table, actions, discount section,
  payment section) plus a **pinned footer** (final total + checkout button) that never scrolls.
- Because the scroll body is never squeezed below its minimum, widgets cannot overlap at any window height; on
  short windows the body scrolls instead.
- Cart table: stretch 1, minimum height 132 (header + 2 rows), short columns `ResizeToContents`, product column
  stretches. Headers "المقاس/اللون" → "مقاس/لون" and "سعر الوحدة" → "السعر" (tooltips carry the full names).
- Actions: `QVBoxLayout` of two `QHBoxLayout`s (3 + 2 buttons), equal stretch, min height 40 / max 46, padding
  reduced to 8x6 px so labels never clip, tooltips on all five buttons; logical order زيادة، تقليل، تعديل الكمية /
  حذف الصنف، تفريغ الفاتورة unchanged (RTL places the first button on the right).
- Hierarchy: title + counters → table → actions → subtotal/discount card → customer/payment/paid/change card →
  total → checkout. Long amounts wrap (word-wrapped labels) instead of clipping.
- Panel width clamp 385-485 kept; stretch 2:3 against the product browser, which has min width 300.

## How to verify (must be done on a machine with PySide6)
```
pip install -r requirements.txt
python scripts/layout_regression_test.py --platform offscreen --out layout_output
```
Produces `POS_1280x780.png`, `POS_1366x768.png`, `POS_1440x900.png`, `POS_1024x768.png`, `POS_1600x900.png`
and `layout_results.json`. Open every PNG and check: no overlapping buttons/totals, readable Arabic, usable
checkout. Also run `python scripts/smoke_test.py --platform offscreen` and `python -m pytest -q`.

## Window sizes covered by the test
1280x780, 1366x768, 1440x900, 1024x768, 1600x900 (the test also exercises worst-case content: 123 units at a
large price, discount, split payment with long "remaining" text).

## Assertions in the test (per size)
Valid non-zero geometry of every required widget; action buttons pairwise disjoint; no action button intersects
subtotal/discount widgets; consistent button height and not below size hint; all invoice-body widgets pairwise
disjoint and hit-testable (`childAt` at centre); checkout and total fully inside the panel and outside the scroll
area; text-clipping via size hints / `heightForWidth` / table header section sizes; every control reachable via
the scroll area; panel and product-browser width bounds.

## Test results
**Not executed in this environment** (see STATUS). Result table to be filled in from `layout_results.json`:

| Size | Result |
|---|---|
| 1280x780 | not run |
| 1366x768 | not run |
| 1440x900 | not run |
| 1024x768 | not run |
| 1600x900 | not run |

## Screenshots
- Before: `docs/screenshots/POS_before_defect.png` (supplied).
- After: **not generated** — produced by the command above.

## Known limitations / risks
- Unverified: spacing and aesthetics may need one tuning round after the first real run.
- At heights around 768 px the panel body is expected to scroll by roughly 100-150 px (customer/payment block
  partly below the fold) because the full control set cannot fit; the total and checkout stay pinned.
  Estimated from size arithmetic, not measured. If you prefer fewer scrolls, the next step is a denser payment block.
- The 650 px minimum window height is unchanged; it works through the scroll area, but is tight.
- The test measures geometry; it cannot judge visual quality, and `offscreen` font metrics can differ from
  Windows (run the same script with `--platform windows` on the target machine).
- Sidebar clipping at low heights and the product-tile grid are outside this task and untouched.
