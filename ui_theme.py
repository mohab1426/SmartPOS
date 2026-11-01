"""SmartPOS visual system (presentation only).

All colours, radii and type sizes come from ``theme_tokens`` (derived from the supplied palette).
This module must not contain raw hexadecimal colours; it does not change persistence, checkout,
inventory, permissions or accounting behaviour.
"""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDateEdit,
    QDoubleSpinBox,
    QLineEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QWidget,
)

from theme_tokens import qss

_TEMPLATE = r"""
/* -- Base canvas and typography ------------------------------------------------ */
QMainWindow, QWidget {
    background-color: @bg@;
    color: @text@;
    font-family: @font_family@;
    font-size: @f_md@px;
}
QWidget#MainContent { background: @bg@; }
QLabel { background: transparent; }
QFrame#WorkspaceHeader { background: @surface@; border: 1px solid @border_soft@; border-radius: @r_xl@px; }
QLabel#WorkspaceEyebrow { color: @text_muted@; font-size: 10px; font-weight: 700; letter-spacing: 1px; }
QLabel#WorkspaceTitle { color: @text@; font-size: @f_lg@px; font-weight: 700; }
QLabel#SystemStatus {
    color: @success@; background: @success_bg@; border: 1px solid @success_border@;
    border-radius: 10px; padding: 7px 12px; font-size: @f_xs@px; font-weight: 700;
}
QLabel#WorkspaceUser {
    color: @text_secondary@; background: @surface_alt@; border: 1px solid @border_soft@;
    border-radius: 10px; padding: 7px 12px; font-weight: 600;
}

/* -- Sidebar --------------------------------------------------------------------- */
QFrame#Sidebar { background: @primary_deep@; border: none; border-left: 1px solid @sidebar_border@; }
QLabel#Brand { color: @text_on_deep@; background: transparent; font-size: 22px; font-weight: 800; padding: 4px 8px 0 8px; min-height: 30px; }
QLabel#SidebarSubtitle { color: @text_on_deep_muted@; background: transparent; font-size: @f_xs@px; padding-bottom: 4px; }
QPushButton#NavToggle {
    color: @text_on_deep@; background: @primary_deep_hover@; border: 1px solid @sidebar_border@;
    border-radius: 10px; padding: 10px 12px; text-align: right; font-size: @f_sm@px; font-weight: 600;
}
QPushButton#NavToggle:hover { background: @sidebar_border@; color: @text_on_deep@; }
QPushButton#NavToggle:pressed { background: @primary_strong@; }
QPushButton#NavButton {
    color: @text_on_deep@; background: transparent; border: 1px solid transparent;
    border-right: 3px solid transparent; border-radius: 10px; padding: 7px 13px;
    text-align: right; font-size: @f_md@px; font-weight: 600; min-height: 27px;
}
QPushButton#NavButton:hover { color: @text_on_deep@; background: @primary_deep_hover@; border-color: @sidebar_border@; }
QPushButton#NavButton:focus { border: 1px solid @primary@; }
QPushButton#NavButton:checked {
    color: @text_on_primary@; background: @primary@; border: 1px solid @primary@;
    border-right: 3px solid @accent@; font-weight: 700;
}
QLabel#SidebarUser {
    color: @text_on_deep@; background: @primary_deep_hover@; border: 1px solid @sidebar_border@;
    border-radius: 11px; padding: 12px 10px; font-size: @f_sm@px; font-weight: 600;
}
QLabel#SidebarVersion { color: @text_on_deep_muted@; background: transparent; padding: 8px; font-size: 10px; }
QFrame#SidebarSeparator { background: @sidebar_border@; max-height: 1px; border: none; }

/* -- Page headings and cards ----------------------------------------------------- */
QLabel#PageTitle { color: @text@; background: transparent; font-size: @f_title@px; font-weight: 800; padding: 2px 0 4px 0; }
QLabel#SectionTitle { color: @text@; background: transparent; font-size: @f_lg@px; font-weight: 700; padding: 7px 0 3px 0; }
QLabel#Muted { color: @text_muted@; background: transparent; font-size: @f_sm@px; }
QFrame#Card, QFrame#InvoicePanel { background: @surface@; border: 1px solid @border_soft@; border-radius: @r_lg@px; }
QFrame#Card:hover { border-color: @border@; }
QLabel#PeriodBanner {
    color: @info@; background: @info_bg@; border: 1px solid @info_border@;
    border-radius: 9px; padding: 9px 12px; font-size: @f_xs@px; font-weight: 600;
}
QLabel#SummaryBanner {
    color: @info@; background: @info_bg@; border: 1px solid @info_border@;
    border-radius: 10px; padding: 12px; font-size: @f_md@px;
}
QLabel#StatusCard {
    color: @text@; background: @surface@; border: 1px solid @border_soft@;
    border-radius: 10px; padding: 14px; font-size: @f_lg@px;
}
QLabel#StatValue { color: @text@; background: transparent; font-size: 23px; font-weight: 700; }
QLabel#FormTotal { color: @text@; background: transparent; font-size: 18px; font-weight: 700; }

/* -- Cashier invoice panel ------------------------------------------------------- */
QScrollArea#InvoiceScroll { background: transparent; border: none; }
QWidget#InvoiceScrollContent { background: transparent; }
QLabel#InvoiceTitle { color: @text@; background: transparent; font-size: 17px; font-weight: 800; padding: 0; }
QLabel#InvoiceChip { color: @text_secondary@; background: @surface_sunken@; border-radius: 7px; padding: 5px 9px; font-size: @f_xs@px; font-weight: 700; }
QFrame#InvoiceSection { background: @surface_alt@; border: 1px solid @border_soft@; border-radius: 10px; }
QLabel#InvoiceFieldLabel { color: @text_secondary@; background: transparent; font-size: @f_xs@px; font-weight: 700; padding: 0; }
QLabel#InvoiceSubtotal { color: @text_secondary@; background: transparent; font-size: @f_md@px; font-weight: 700; padding: 2px 0; }
QLabel#InvoiceDiscountValue { color: @warning@; background: transparent; font-size: @f_sm@px; font-weight: 700; padding: 2px 0; }
QLabel#InvoiceChange { color: @primary_strong@; background: transparent; font-size: @f_sm@px; font-weight: 700; padding: 2px 0; }
QLabel#InvoiceTotal {
    color: @text@; background: @primary_tint@; border: 1px solid @primary@;
    border-radius: 10px; padding: 8px 10px; font-size: @f_xl@px; font-weight: 800;
}

/* -- Buttons --------------------------------------------------------------------- */
QPushButton {
    color: @text@; background: @surface@; border: 1px solid @border@;
    border-radius: @r_md@px; padding: 9px 13px; min-height: 20px; font-size: @f_sm@px; font-weight: 600;
}
QPushButton:hover { background: @surface_sunken@; border-color: @border_strong@; }
QPushButton:pressed { background: @kidnapper@; }
QPushButton:focus { border: 2px solid @focus@; padding: 8px 12px; }
QPushButton:disabled { color: @text_disabled@; background: @surface_alt@; border-color: @border_soft@; }
QPushButton#Primary {
    color: @text_on_accent@; background: @accent@; border: 1px solid @accent@;
    border-radius: @r_md@px; padding: 10px 16px; font-weight: 700;
}
QPushButton#Primary:hover { background: @accent_hover@; border-color: @accent_hover@; }
QPushButton#Primary:pressed { background: @accent_pressed@; }
QPushButton#Primary:focus { border: 2px solid @text@; padding: 9px 15px; }
QPushButton#Primary:disabled { color: @text_disabled@; background: @surface_sunken@; border-color: @border@; }
QPushButton#Danger {
    color: @text_on_deep@; background: @error@; border: 1px solid @error@;
    border-radius: @r_md@px; padding: 9px 14px; font-weight: 700;
}
QPushButton#Danger:hover { background: @error_hover@; border-color: @error_hover@; }
QPushButton#Success { color: @text_on_deep@; background: @success@; border: 1px solid @success@; font-weight: 700; padding: 10px; }
QPushButton#Warn { color: @text_on_accent@; background: @accent_alt@; border: 1px solid @accent_alt@; font-weight: 700; padding: 10px; }
QPushButton#Ghost { background: transparent; border: 1px solid transparent; color: @primary_strong@; padding: 6px 10px; }
QPushButton#Ghost:hover { background: @primary_tint@; }
QPushButton#ProductTile {
    color: @text@; background: @surface@; border: 1px solid @border_soft@; border-radius: 13px;
    padding: 14px 10px; text-align: center; min-height: 82px; font-size: @f_sm@px; font-weight: 600;
}
QPushButton#ProductTile:hover { color: @text@; background: @primary_tint@; border: 1px solid @primary@; }
QPushButton#CategoryButton {
    color: @text_secondary@; background: @surface@; border: 1px solid @border@;
    border-radius: @r_pill@px; padding: 8px 15px; min-height: 18px;
}
QPushButton#CategoryButton:hover { background: @secondary_tint@; border-color: @secondary@; }
QPushButton#CategoryButton:checked { color: @text_on_primary@; background: @primary@; border: 1px solid @primary@; font-weight: 700; }

/* -- Inputs ---------------------------------------------------------------------- */
QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QDateEdit {
    color: @text@; background: @surface@; border: 1px solid @border@; border-radius: @r_md@px;
    padding: 8px 10px; selection-background-color: @primary@; selection-color: @text_on_primary@; min-height: 22px;
}
QLineEdit:hover, QSpinBox:hover, QDoubleSpinBox:hover, QComboBox:hover, QDateEdit:hover { border-color: @border_strong@; }
QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus, QDateEdit:focus {
    border: 2px solid @focus@; padding: 7px 9px; background: @surface@;
}
QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled, QDateEdit:disabled {
    color: @text_disabled@; background: @surface_alt@;
}
QLineEdit[invalid="true"] { border: 2px solid @error@; padding: 7px 9px; }
QComboBox::drop-down, QDateEdit::drop-down { border: none; width: 27px; }
QComboBox QAbstractItemView {
    color: @text@; background: @surface@; border: 1px solid @border@;
    selection-background-color: @primary_tint@; selection-color: @text@; outline: none; padding: 5px;
}
QComboBox QAbstractItemView::item { min-height: 27px; padding: 5px 8px; border-radius: 5px; }
QCheckBox { color: @text_secondary@; spacing: 8px; }
QCheckBox::indicator { width: 17px; height: 17px; }
QCheckBox::indicator:unchecked { background: @surface@; border: 1px solid @border_strong@; border-radius: 5px; }
QCheckBox::indicator:checked { background: @primary_strong@; border: 1px solid @primary_strong@; border-radius: 5px; }

/* -- Tables ---------------------------------------------------------------------- */
QTableWidget {
    color: @text@; background: @surface@; alternate-background-color: @surface_alt@;
    border: 1px solid @border_soft@; border-radius: @r_lg@px; gridline-color: transparent;
    selection-background-color: @primary_tint@; selection-color: @text@; outline: none;
}
QTableWidget::item { padding: 8px 7px; border: none; border-bottom: 1px solid @border_soft@; }
QTableWidget::item:selected { color: @text@; background: @primary@; }
QTableWidget::item:hover { background: @primary_tint@; }
QHeaderView::section {
    color: @text_secondary@; background: @surface_sunken@; border: none; border-bottom: 1px solid @border@;
    padding: 11px 8px; font-size: @f_xs@px; font-weight: 700;
}
QTableCornerButton::section { background: @surface_sunken@; border: none; }

/* -- Scrollbars, dialogs and feedback ------------------------------------------ */
QScrollArea { border: none; background: transparent; }
QScrollBar:vertical { background: transparent; width: 10px; margin: 3px; }
QScrollBar::handle:vertical { background: @border_strong@; border-radius: 5px; min-height: 28px; }
QScrollBar::handle:vertical:hover { background: @primary_strong@; }
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
QScrollBar:horizontal { background: transparent; height: 10px; margin: 3px; }
QScrollBar::handle:horizontal { background: @border_strong@; border-radius: 5px; min-width: 28px; }
QScrollBar::handle:horizontal:hover { background: @primary_strong@; }
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
QDialog { background: @bg@; }
QMessageBox { background: @elevated@; }
QToolTip { color: @text_on_deep@; background: @primary_deep@; border: 1px solid @sidebar_border@; border-radius: 6px; padding: 6px 8px; }

/* -- Login / first-run setup ---------------------------------------------------- */
QDialog#LoginDialog { background: @bg@; }
QWidget#FieldBox { background: transparent; }
QFrame#LoginCard { background: @elevated@; border: 1px solid @border_soft@; border-radius: @r_xl@px; }
QLabel#LoginLogo {
    color: @text_on_primary@; background: @primary@; border-radius: 16px; font-size: 24px; font-weight: 800;
}
QLabel#LoginTitle { color: @text@; background: transparent; font-size: 24px; font-weight: 800; }
QLabel#LoginSubtitle { color: @text_secondary@; background: transparent; font-size: @f_md@px; }
QLabel#FieldLabel { color: @text_secondary@; background: transparent; font-size: @f_sm@px; font-weight: 700; }
QLabel#FieldHint { color: @text_muted@; background: transparent; font-size: @f_xs@px; }
QLabel#FormError {
    color: @error@; background: @error_bg@; border: 1px solid @error_border@;
    border-radius: 9px; padding: 9px 12px; font-size: @f_sm@px; font-weight: 600;
}
QLabel#FormInfo {
    color: @info@; background: @info_bg@; border: 1px solid @info_border@;
    border-radius: 9px; padding: 9px 12px; font-size: @f_sm@px;
}
QPushButton#LoginPrimary {
    color: @text_on_accent@; background: @accent@; border: 1px solid @accent@;
    border-radius: 10px; padding: 12px 16px; font-size: @f_lg@px; font-weight: 800; min-height: 24px;
}
QPushButton#LoginPrimary:hover { background: @accent_hover@; border-color: @accent_hover@; }
QPushButton#LoginPrimary:pressed { background: @accent_pressed@; }
QPushButton#LoginPrimary:focus { border: 2px solid @text@; padding: 11px 15px; }
QPushButton#LoginPrimary:disabled { color: @text_secondary@; background: @surface_sunken@; border-color: @border@; }
QPushButton#PasswordToggle { background: transparent; border: none; color: @primary_strong@; padding: 0 8px; font-weight: 700; }
QPushButton#PasswordToggle:hover { background: @primary_tint@; }
"""

APP_STYLESHEET = qss(_TEMPLATE)


def polish_widget_tree(root: QWidget) -> None:
    """Apply consistent interaction and sizing defaults without changing logic."""
    for widget in root.findChildren(QWidget):
        if isinstance(widget, QPushButton):
            widget.setCursor(Qt.CursorShape.PointingHandCursor)
            widget.setMinimumHeight(max(widget.minimumHeight(), 38))
        elif isinstance(widget, (QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit)):
            widget.setMinimumHeight(max(widget.minimumHeight(), 39))
        elif isinstance(widget, QTableWidget):
            widget.setAlternatingRowColors(True)
            widget.setShowGrid(False)
            widget.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            widget.verticalHeader().setVisible(False)
            widget.verticalHeader().setDefaultSectionSize(42)
            widget.horizontalHeader().setMinimumHeight(43)
            widget.horizontalHeader().setHighlightSections(False)
            widget.setWordWrap(False)
            widget.setSortingEnabled(False)
