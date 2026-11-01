# Cashier invoice panel — layout contract

```
invoice_panel  (QFrame#InvoicePanel, width 385-485, stretch 2 vs product browser stretch 3)
 └ QVBoxLayout (margins 12, spacing 8)
    ├ invoice_scroll (QScrollArea, vertical only, min height 160, stretch 1)
    │   └ invoice_body (QVBoxLayout, spacing 8)
    │       ├ header row: title + item/unit counter chip
    │       ├ cart_table (stretch 1, min height 132 = header + 2 rows)
    │       ├ cart actions: QVBoxLayout of 2 QHBoxLayouts (3 + 2 buttons, equal stretch, min height 40)
    │       ├ discount section: subtotal label, discount input, discount value label
    │       └ payment section: customer, payment method, paid amount, change/remaining
    └ footer (never scrolls): final total label + checkout button
```
Rules: nothing in the scroll body is ever laid out below its minimum size (the scroll area scrolls instead);
the final total and checkout button are outside the scroll area; long numeric texts wrap instead of clipping.
Styles live in `ui_theme.py` (`Invoice*` object names). Do not re-introduce inline `setStyleSheet` sizes or
fixed geometries here.
