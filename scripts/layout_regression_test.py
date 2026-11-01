"""SmartPOS cashier layout regression test (real window, real widget geometry).

Launches the REAL application window against an isolated temporary database, logs in through
the real dialog, opens the cashier screen, resizes the window to every required size, lets Qt
finish its layout passes and then inspects the actual widget rectangles:

* cart action buttons never intersect each other, the subtotal label or any discount widget;
* every stacked widget in the invoice body is disjoint from every other one (and a hit-test at
  each widget's centre really lands on that widget, so nothing is covered);
* no required widget has zero / negative / out-of-parent geometry;
* Arabic text is not clipped (size hints, wrapped-label heights, table header sections);
* the checkout button and final total are always inside the panel, and every control inside the
  scroll area can be scrolled fully into view;
* the product browser is not squeezed below a usable width.

Usage:
    python scripts/layout_regression_test.py [--out DIR] [--platform offscreen|windows]

Exit code 0 = all checks passed.  Screenshots POS_<W>x<H>.png are written to --out.
Nothing here touches a real database; no business logic is exercised (the cart is only
populated in memory, checkout is never invoked).
"""
from __future__ import annotations

import argparse
import json
import os
import sys

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ADMIN_USER, ADMIN_PASS = "layouttest", "LayoutTest-12345"
SIZES = [(1280, 780), (1366, 768), (1440, 900), (1024, 768), (1600, 900)]
RESULTS: list[dict] = []


def record(size: str, name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append({"size": size, "check": name, "passed": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {size:>9} | {name}" + (f" - {detail}" if detail else ""), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default=str(ROOT / "layout_output"))
    parser.add_argument("--platform", default=None, help="Qt platform plugin (e.g. offscreen)")
    args = parser.parse_args()
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    if args.platform:
        os.environ["QT_QPA_PLATFORM"] = args.platform

    data_dir = Path(tempfile.mkdtemp(prefix="smartpos_layout_"))
    os.environ["SMARTPOS_DATA_DIR"] = str(data_dir)
    os.environ["XDG_CONFIG_HOME"] = str(data_dir / "cfg")
    os.environ["APPDATA"] = str(data_dir / "appdata")
    sys.path.insert(0, str(ROOT))

    from PySide6.QtCore import QPoint, QRect, QTimer
    from PySide6.QtGui import QFontMetrics
    from PySide6.QtWidgets import (QApplication, QDialog, QLabel, QLineEdit, QMessageBox,
                                   QPushButton, QWidget)

    import database
    from services.auth import create_first_admin
    from services.sales import PAYMENT_SPLIT

    assert database.DATA_DIR == data_dir.resolve(), "isolation failed: not using the temp data dir"
    database.initialize_database()
    with database.get_connection() as con:
        create_first_admin(con, username=ADMIN_USER, display_name="Layout Admin", password=ADMIN_PASS)
        pid = con.execute(
            "INSERT INTO products(code,name,purchase_price,selling_price,stock_quantity,min_stock) "
            "VALUES('LAYOUT-1','صنف اختبار بإسم طويل نسبيًا للتحقق من القص',500,1200,500,0)").lastrowid
        con.execute(
            "INSERT INTO inventory_movements(product_id,movement_type,quantity_delta,unit_cost,"
            "reference_type,reference_id,reason) VALUES(?,'opening_balance',500,500,'layout',?,'layout fixture')",
            (pid, pid))
        con.commit()

    app = QApplication.instance() or QApplication(sys.argv)
    state = {"login_done": False, "ticks": 0}

    def pump() -> None:
        state["ticks"] += 1
        modal = QApplication.activeModalWidget()
        if modal is None:
            return
        if state["ticks"] > 400 and isinstance(modal, QDialog) and not state["login_done"]:
            print("[FAIL] watchdog: login dialog did not offer two line edits", flush=True)
            os._exit(2)                      # never hang CI on an unexpected modal dialog
        if isinstance(modal, QMessageBox):
            modal.accept()
        elif isinstance(modal, QDialog):
            edits = modal.findChildren(QLineEdit)
            if len(edits) >= 2 and not state["login_done"]:
                state["login_done"] = True
                edits[0].setText(ADMIN_USER)
                edits[1].setText(ADMIN_PASS)
                for btn in modal.findChildren(QPushButton):
                    if btn.objectName() in ("Primary", "LoginPrimary"):
                        btn.click()
                        break

    timer = QTimer()
    timer.setInterval(150)
    timer.timeout.connect(pump)
    timer.start()

    import app as smartpos_app

    window = smartpos_app.SmartPOS()
    if getattr(window, "_login_cancelled", True):
        record("setup", "login through the real dialog", False)
        return finish(out)
    record("setup", "login through the real dialog", True)

    with database.get_connection() as con:
        from services.shifts import open_shift
        open_shift(con, user=window.current_user, opening_cash=10000)

    window.show()
    window.show_cashier()

    # Realistic, worst-case-ish content: several lines, big numbers, split payment (long change text).
    window.add_product_to_cart(pid)
    window.cart[pid]["quantity"] = 123
    window.cart[pid]["price"] = 123456789          # piasters, in-memory only
    window.discount_input.setValue(12345.67)
    window.payment_method.setCurrentText(PAYMENT_SPLIT)
    window.paid_input.setValue(1234567.89)
    window.render_cart(selected_pid=pid)

    # ------------------------------------------------------------------ helpers
    def settle() -> None:
        """Let Qt finish every pending layout / resize / polish pass before measuring."""
        for _ in range(6):
            app.processEvents()
            for w in (window.centralWidget(), window.cashier_page, window.invoice_panel, window.invoice_body):
                lay = w.layout()
                if lay is not None:
                    lay.invalidate()
                    lay.activate()
            app.processEvents()

    def check_cart_columns(tag: str, label: str = "") -> None:
        """Real-geometry checks of the cart columns (no implementation constants besides the floor)."""
        table = window.cart_table
        header = table.horizontalHeader()
        sfx = f" ({label})" if label else ""
        visible = [c for c in range(table.columnCount()) if not table.isColumnHidden(c)]
        cut = [f"col{c}: {header.sectionSize(c)}<{header.sectionSizeHint(c)}"
               for c in visible if header.sectionSize(c) < header.sectionSizeHint(c)]
        record(tag, "cart header labels are not clipped" + sfx, not cut, "; ".join(cut))
        name_w = header.sectionSize(1)
        record(tag, "cart product-name column keeps a readable width" + sfx,
               name_w >= smartpos_app.CartColumnFitter.NAME_FLOOR,
               f"col1={name_w}px (floor {smartpos_app.CartColumnFitter.NAME_FLOOR}px)")
        elided = [f"col{c}: {header.sectionSize(c)}<{table.sizeHintForColumn(c)}"
                  for c in (3, 5) if header.sectionSize(c) < table.sizeHintForColumn(c)]
        record(tag, "cart money columns are not elided" + sfx, not elided, "; ".join(elided))
        # The horizontal scrollbar must appear exactly when the columns cannot fit.
        viewport_w = table.viewport().width()
        needs_scroll = header.length() > viewport_w
        bar_visible = table.horizontalScrollBar().isVisible()
        record(tag, "cart horizontal scrollbar only when columns do not fit" + sfx,
               bar_visible == needs_scroll,
               f"columns={header.length()}px viewport={viewport_w}px scrollbar_visible={bar_visible}")

    def rect_in(widget: QWidget, ancestor: QWidget) -> QRect:
        return QRect(widget.mapTo(ancestor, QPoint(0, 0)), widget.size())

    def hit_ok(widget: QWidget, container: QWidget) -> bool:
        """The top-most child of `container` at the widget's centre must be the widget (or its child)."""
        centre = rect_in(widget, container).center()
        hit = container.childAt(centre)
        node = hit
        while node is not None:
            if node is widget:
                return True
            node = node.parentWidget()
        return False

    body = window.invoice_body
    panel = window.invoice_panel
    scroll = window.invoice_scroll
    footer_widgets = {"الإجمالي المطلوب": window.total_label, "زر الحفظ": window.checkout_button}
    buttons = list(window.cart_action_buttons)
    button_names = ["زيادة", "تقليل", "تعديل الكمية", "حذف الصنف", "تفريغ الفاتورة"]
    discount_widgets = {"خانة الخصم": window.discount_input, "قيمة الخصم": window.discount_summary_label}
    body_widgets = {
        "عنوان الفاتورة": window.cart_title_label,
        "ملخص الأصناف": window.cart_summary_label,
        "جدول السلة": window.cart_table,
        **{f"زر {n}": b for n, b in zip(button_names, buttons)},
        "الإجمالي قبل الخصم": window.subtotal_label,
        **discount_widgets,
        "عنوان العميل": window.customer_label,
        "قائمة العميل": window.customer_combo,
        "عنوان طريقة الدفع": window.payment_label,
        "طريقة الدفع": window.payment_method,
        "المدفوع": window.paid_input,
        "الباقي": window.change_label,
    }

    failures_total = 0
    for width, height in SIZES:
        tag = f"{width}x{height}"
        window.showNormal()
        window.resize(width, height)
        settle()
        window.invoice_scroll.verticalScrollBar().setValue(0)
        settle()
        before = len(RESULTS)

        record(tag, "window reached the requested size",
               (window.width(), window.height()) == (width, height), f"actual {window.width()}x{window.height()}")

        # ---- 1. valid geometry for every required widget --------------------------------
        bad = []
        for name, w in {**body_widgets, **footer_widgets}.items():
            r = w.geometry()
            parent_rect = w.parentWidget().rect()
            if (not w.isVisible() or r.width() <= 0 or r.height() <= 0
                    or r.x() < 0 or r.y() < 0 or r.right() > parent_rect.right() + 1):
                bad.append(f"{name}:{r.x()},{r.y()},{r.width()}x{r.height()}")
        record(tag, "no required widget has zero / invalid / out-of-parent geometry", not bad, "; ".join(bad))

        # ---- 2. cart action buttons ------------------------------------------------------
        rects = [rect_in(b, body) for b in buttons]
        pairs = [(button_names[i], button_names[j]) for i in range(len(buttons)) for j in range(i + 1, len(buttons))
                 if rects[i].intersects(rects[j])]
        record(tag, "cart action buttons do not intersect one another", not pairs, str(pairs))
        sub_rect = rect_in(window.subtotal_label, body)
        hits = [n for n, r in zip(button_names, rects) if r.intersects(sub_rect)]
        record(tag, "no cart action button intersects the subtotal label", not hits, str(hits))
        disc_hits = [(bn, dn) for bn, r in zip(button_names, rects) for dn, dw in discount_widgets.items()
                     if r.intersects(rect_in(dw, body))]
        record(tag, "no cart action button intersects the discount widgets", not disc_hits, str(disc_hits))
        heights = {b.height() for b in buttons}
        record(tag, "cart action buttons have a consistent height", len(heights) == 1, str(sorted(heights)))
        record(tag, "cart action buttons are not shorter than their size hint",
               all(b.height() >= b.sizeHint().height() for b in buttons),
               str([(n, b.height(), b.sizeHint().height()) for n, b in zip(button_names, buttons)
                    if b.height() < b.sizeHint().height()]))

        # ---- 3. labels / inputs / buttons: disjoint and not covered ----------------------
        names = list(body_widgets)
        rr = {n: rect_in(body_widgets[n], body) for n in names}
        overlaps = [(a, b) for i, a in enumerate(names) for b in names[i + 1:] if rr[a].intersects(rr[b])]
        record(tag, "labels, inputs and buttons in the invoice body do not overlap", not overlaps, str(overlaps))
        covered = [n for n in names if not hit_ok(body_widgets[n], body)]
        record(tag, "every invoice-body widget is hit-testable (nothing covers it)", not covered, str(covered))

        # ---- 4. footer (total + checkout) is always visible, never under the scroll area --
        panel_rect = panel.rect()
        foot = {n: rect_in(w, panel) for n, w in footer_widgets.items()}
        inside = all(panel_rect.contains(r) for r in foot.values())
        record(tag, "checkout button and total are fully inside the invoice panel", inside, str(foot))
        scroll_rect = rect_in(scroll, panel)
        under = [n for n, r in foot.items() if r.intersects(scroll_rect)]
        record(tag, "footer does not overlap the scroll area", not under, str(under))
        record(tag, "checkout button is hit-testable", hit_ok(window.checkout_button, panel))
        record(tag, "checkout button is visible without scrolling",
               window.checkout_button.isVisible() and window.checkout_button.isEnabled())

        # ---- 5. clipping ------------------------------------------------------------------
        clipped = []
        for name, w in {**body_widgets, **footer_widgets}.items():
            if name == "جدول السلة":
                continue
            if isinstance(w, QLabel) and w.wordWrap():
                if w.heightForWidth(w.width()) > w.height():
                    clipped.append(f"{name}: needs {w.heightForWidth(w.width())}px, has {w.height()}px")
            else:
                hint = w.sizeHint()
                if hint.width() > w.width() or hint.height() > w.height():
                    clipped.append(f"{name}: hint {hint.width()}x{hint.height()} > {w.width()}x{w.height()}")
        record(tag, "no label / input / button text is clipped", not clipped, "; ".join(clipped))

        header = window.cart_table.horizontalHeader()
        cut = []
        for col in range(window.cart_table.columnCount()):
            if window.cart_table.isColumnHidden(col):
                continue
            if header.sectionSize(col) < header.sectionSizeHint(col):
                cut.append(f"col{col}: {header.sectionSize(col)}<{header.sectionSizeHint(col)}")
        record(tag, "cart table header labels are not clipped", not cut, "; ".join(cut))

        check_cart_columns(tag)

        fm = QFontMetrics(window.total_label.font())
        text_w = fm.horizontalAdvance(window.total_label.text())
        record(tag, "final total text fits (single line or wrapped inside its box)",
               text_w <= window.total_label.width() or window.total_label.heightForWidth(window.total_label.width())
               <= window.total_label.height())

        # ---- 6. everything inside the scroll area can be scrolled fully into view ---------
        unreachable = []
        vp = scroll.viewport()
        bar = scroll.verticalScrollBar()

        for name, w in body_widgets.items():
            app.processEvents()

            widget_top = w.mapTo(body, QPoint(0, 0)).y()
            widget_bottom = widget_top + w.height()

            target = bar.value()

            if widget_top < target:
                target = widget_top
            elif widget_bottom > target + vp.height():
                target = widget_bottom - vp.height()

            bar.setValue(max(bar.minimum(), min(target, bar.maximum())))

            app.processEvents()
            settle()

            r = QRect(w.mapTo(vp, QPoint(0, 0)), w.size())
            ok = r.intersects(vp.rect()) if name == "جدول السلة" else vp.rect().contains(r)

            if not ok:
                parent_chain = []
                node = w
                while node is not None:
                    parent_chain.append(
                        f"{node.objectName() or node.__class__.__name__}"
                    )
                    node = node.parentWidget()

                print(
                    f"\n[SCROLL DEBUG] {name}"
                    f"\n  descendant_of_body={body.isAncestorOf(w)}"
                    f"\n  rect={r.getRect()}"
                    f"\n  viewport={vp.rect().getRect()}"
                    f"\n  scrollbar={bar.value()}/{bar.maximum()}"
                    f"\n  body_size={body.size().width()}x{body.size().height()}"
                    f"\n  parent_chain={' -> '.join(parent_chain)}"
                )

                unreachable.append(
                    f"{name}: {r.getRect()} vs viewport {vp.rect().getRect()}"
                )

        record(
            tag,
            "every control is reachable through the intended scroll container",
            not unreachable,
            "; ".join(unreachable),
        )
        bar.setValue(0)
        settle()

        # ---- 7. panel / product browser proportions ---------------------------------------
        page_rect = window.cashier_page.rect()
        panel_in_page = rect_in(panel, window.cashier_page)
        record(tag, "invoice panel stays inside the cashier page", page_rect.contains(panel_in_page), str(panel_in_page))
        record(tag, "invoice panel width within 385-485 px", 385 <= panel.width() <= 485, f"{panel.width()}px")
        record(tag, "product browser is not unusably narrow",
               window.products_scroll.width() >= 300, f"{window.products_scroll.width()}px")
        record(tag, "cart table keeps at least header + two rows",
               window.cart_table.height() >= 132, f"{window.cart_table.height()}px")

        window.grab().save(str(out / f"POS_{tag}.png"))
        new = RESULTS[before:]
        failures_total += sum(1 for r in new if not r["passed"])

    # ---- extra cart scenarios at the last size: extremely long product name, then empty cart ----
    tag = "extra"
    window.cart[pid]["name"] = "اسم منتج طويل جدًا للغاية " * 12
    window.render_cart(selected_pid=pid)
    settle()
    check_cart_columns(tag, "very long product name")
    window.cart.clear()
    window.render_cart()
    settle()
    check_cart_columns(tag, "empty cart")
    failures_total += sum(1 for r in RESULTS if r["size"] == tag and not r["passed"])

    window.close()
    window.deleteLater()
    app.processEvents()
    return finish(out, failures_total)


def finish(out: Path, extra_failures: int = 0) -> int:
    failed = [r for r in RESULTS if not r["passed"]]
    (out / "layout_results.json").write_text(json.dumps(RESULTS, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} layout checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    code = main()
    sys.stdout.flush()
    os._exit(code)
