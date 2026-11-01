import sys
import sqlite3
import uuid
import html
import os
import json
import logging
from pathlib import Path
from datetime import datetime, timedelta

from PySide6.QtCore import Qt, QDate, QSizeF, QMarginsF, QTimer, QThread, Signal, QObject, QEvent
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QLabel,
    QPushButton, QStackedWidget, QFrame, QTableWidget, QTableWidgetItem,
    QHeaderView, QMessageBox, QDialog, QLineEdit, QFormLayout, QDoubleSpinBox,
    QSpinBox, QComboBox, QAbstractItemView, QInputDialog, QGridLayout, QScrollArea, QSizePolicy, QDateEdit, QFileDialog, QCheckBox
)
from PySide6.QtGui import QTextDocument, QPdfWriter, QPageSize, QPageLayout, QPainter, QPen, QBrush, QColor, QFont
from PySide6.QtPrintSupport import QPrinter, QPrintDialog

from database import (get_connection, initialize_database, DATABASE_PATH,
                      CONFIG_DIR, BACKUP_DIR, LOG_DIR, MIGRATION_BACKUPS_DIR)
import startup_guard
from ui_theme import APP_STYLESHEET, polish_widget_tree
import theme_tokens as T
from login_dialog import FirstRunDialog, LoginDialog
from app_version import APP_VERSION
from services.accounting import calculate_expected_cash, business_date_for, unclassified_refunds_for_shift
from services.customers import (create_customer, find_customers_by_name,
                                build_customer_labels, customer_balance,
                                collect_customer_payment)
from services.sales import (create_sale, process_return, classify_legacy_return,
                            PAYMENT_CASH, PAYMENT_CREDIT, PAYMENT_SPLIT,
                            allocate_net_line_values, calculate_line_return_refund)
from services.reporting import get_report_snapshot
from services.backup import create_database_backup, restore_database_file, run_backup_job, validate_database_file
from services.security import is_admin
from services.audit import write_audit_log
from services import catalog as catalog_service
from services.auth import (authenticate, create_first_admin, create_user, hash_password,
                           reset_user_password as service_reset_password, set_user_active, verify_password)
from services.money import to_piasters
from services.purchasing import receive_purchase
from services.inventory import UnknownCostError
from services.shifts import add_cash_movement as service_add_cash_movement
from services.shifts import close_shift as service_close_shift
from services.shifts import open_shift as service_open_shift


def money(piasters):
    """Format integer piasters without binary-float rounding artifacts."""
    amount = int(piasters or 0)
    sign = "-" if amount < 0 else ""
    whole, fraction = divmod(abs(amount), 100)
    return f"{sign}{whole:,}.{fraction:02d} جنيه"


class FocusSelectDoubleSpinBox(QDoubleSpinBox):
    """Select the current numeric value when the field receives focus."""
    def focusInEvent(self, event):
        super().focusInEvent(event)
        QTimer.singleShot(0, self.lineEdit().selectAll)


class ReportBarChart(QWidget):
    """Small dependency-free chart for the reports dashboard."""
    def __init__(self, title="", parent=None):
        super().__init__(parent)
        self.title = title
        self.items = []
        self.bar_color = QColor(T.PRIMARY_STRONG)
        self.value_divisor = 100.0
        self.setMinimumHeight(220)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def set_data(self, items, color=T.PRIMARY_STRONG, value_divisor=100.0):
        self.items = [(str(label), float(value or 0)) for label, value in items]
        self.bar_color = QColor(color)
        self.value_divisor = value_divisor if value_divisor else 1.0
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.fillRect(self.rect(), QColor(T.SURFACE))
        painter.setPen(QColor(T.TEXT))
        font = QFont("Segoe UI", 11); font.setBold(True); painter.setFont(font)
        painter.drawText(16, 24, self.title)
        if not self.items:
            painter.setPen(QColor(T.TEXT_MUTED)); painter.setFont(QFont("Segoe UI", 10))
            painter.drawText(self.rect().adjusted(10, 35, -10, -10), Qt.AlignmentFlag.AlignCenter, "لا توجد بيانات في الفترة المحددة")
            painter.end(); return
        left, right, top, bottom = 42, self.width()-16, 42, self.height()-43
        chart_h = max(40, bottom-top)
        max_value = max([abs(v) for _, v in self.items] + [1.0])
        painter.setPen(QPen(QColor(T.BORDER_SOFT), 1))
        for i in range(1, 4):
            y = top + int(chart_h*i/4)
            painter.drawLine(left, y, right, y)
        n = len(self.items); slot = max(1, (right-left)//n); bar_w = max(6, min(34, int(slot*0.56)))
        label_font = QFont("Segoe UI", 8); painter.setFont(label_font)
        for i, (label, value) in enumerate(self.items):
            x_center = left + int(slot*(i+0.5))
            h = int(abs(value)/max_value*(chart_h-8))
            y = bottom-h
            color = self.bar_color if value >= 0 else QColor(T.ERROR)
            painter.setPen(Qt.PenStyle.NoPen); painter.setBrush(QBrush(color))
            painter.drawRoundedRect(x_center-bar_w//2, y, bar_w, max(3,h), 3, 3)
            painter.setPen(QColor(T.TEXT_MUTED))
            short_label = label if len(label) <= 9 else label[-9:]
            painter.drawText(x_center-slot//2, bottom+5, slot, 20, Qt.AlignmentFlag.AlignHCenter|Qt.AlignmentFlag.AlignTop, short_label)
            if slot >= 48:
                val_text = f"{value/self.value_divisor:,.0f}"
                painter.drawText(x_center-slot//2, max(top-1,y-17), slot, 16, Qt.AlignmentFlag.AlignHCenter|Qt.AlignmentFlag.AlignVCenter, val_text)
        painter.end()


class CartColumnFitter(QObject):
    """Keeps the cashier cart columns readable at every window size.

    Layout contract for the cart table (columns: 0 hidden id, 1 product, 2 size/colour, 3 price,
    4 quantity, 5 line total):

    * columns 2-5 are Interactive and are sized from their real content (never elided: money values
      must never be cut), with the header label width as a floor;
    * column 1 (product name) is the only Stretch column and receives whatever width remains;
    * the header minimum section size is the widest header label, so no visible header label can be
      clipped, and the product name can never collapse to a sliver;
    * when the content genuinely cannot fit (very long amounts in a narrow panel) the table scrolls
      horizontally instead of squeezing the product name below ``NAME_FLOOR``.  The checkout
      controls live outside this table, so they stay reachable.

    The fit is re-applied whenever the cart is rendered and whenever the table viewport is resized.
    """

    NAME_TARGET = 110     # comfortable product-name width (px)
    NAME_FLOOR = 70       # absolute minimum before horizontal scrolling takes over
    CELL_PADDING = 22     # horizontal padding added to measured cell content (px)
    FIXED_COLUMNS = (2, 3, 4, 5)

    def __init__(self, table: QTableWidget):
        super().__init__(table)
        self.table = table
        self._busy = False
        header = table.horizontalHeader()
        header.setStretchLastSection(False)
        for column in self.FIXED_COLUMNS:
            header.setSectionResizeMode(column, QHeaderView.ResizeMode.Interactive)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        table.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        table.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        table.viewport().installEventFilter(self)

    def eventFilter(self, obj, event):
        try:
            if obj is self.table.viewport() and event.type() == QEvent.Type.Resize:
                self.fit()
        except RuntimeError:        # table already destroyed during interpreter/window shutdown
            pass
        return False

    def fit(self):
        """Apply the column plan.  Re-entrant calls are ignored while a pass runs, but if the
        viewport width changed during the pass (for example a vertical scrollbar appeared because
        the horizontal one reduced the height) the pass is repeated, at most three times."""
        if self._busy:
            return
        self._busy = True
        try:
            for _ in range(3):
                start_width = self.table.viewport().width()
                self._fit_once()
                if self.table.viewport().width() == start_width:
                    break
        finally:
            self._busy = False

    def _fit_once(self):
        table, header = self.table, self.table.horizontalHeader()
        visible = [c for c in range(table.columnCount()) if not table.isColumnHidden(c)]
        if not visible:
            return
        hints = {c: header.sectionSizeHint(c) for c in visible}
        widest_label = max(hints.values())
        # Header labels may never be clipped: the smallest allowed section is the widest label.
        header.setMinimumSectionSize(widest_label)
        width = table.viewport().width()
        if width <= 0:
            return
        need = {}
        for c in self.FIXED_COLUMNS:
            if c in hints:
                # sizeHintForColumn() is 0 for an empty cart, which leaves the header label width.
                need[c] = max(hints[c], table.sizeHintForColumn(c) + self.CELL_PADDING)
        name_width = width - sum(need.values())
        if name_width < self.NAME_TARGET and 2 in need:
            # Variant text is the least important column: shrink it to its header label first.
            need[2] = hints[2]
            name_width = width - sum(need.values())
        for column, column_width in need.items():
            if header.sectionSize(column) != column_width:
                header.resizeSection(column, column_width)
        # Column 1 is Stretch: Qt gives it the remainder, never less than the minimum section size.
        # If the remainder is below NAME_FLOOR, raise the minimum so the name stays readable and
        # let the table scroll horizontally instead.
        if name_width < self.NAME_FLOOR:
            header.setMinimumSectionSize(max(widest_label, self.NAME_FLOOR))


class AutoBackupThread(QThread):
    """Runs the periodic backup (copy + integrity check) off the GUI thread so the cashier screen never freezes."""
    finished_with = Signal(object, object)          # (backup path or None, error text or None)

    def __init__(self, database_path, backup_dir, parent=None):
        super().__init__(parent)
        self._database_path, self._backup_dir = str(database_path), str(backup_dir)

    def run(self):
        path, error = run_backup_job(self._database_path, self._backup_dir)
        self.finished_with.emit(str(path) if path else None, error)


class SmartPOS(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setStyleSheet(APP_STYLESHEET)
        initialize_database()
        logging.basicConfig(
            filename=str(LOG_DIR / "smartpos.log"),
            level=logging.INFO,
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
            encoding="utf-8",
        )
        self.logger = logging.getLogger("smartpos")
        self.current_user = None
        if not self.setup_or_login():
            self._login_cancelled = True
            return
        self._login_cancelled = False
        self.cart = {}  # must exist before build_interface(): cashier page computes totals while being built
        self.setWindowTitle("Smart POS | نظام الكاشير والمخزون")
        self.resize(1280, 780)
        self.setMinimumSize(1000, 650)
        self.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        self.build_interface()
        polish_widget_tree(self)
        self.setup_backup_system()
        self.apply_role_permissions()
        self.show_cashier() if self.current_user["role"] == "cashier" else self.show_home()

    @staticmethod
    def password_hash(password, salt=None):
        return hash_password(password, salt)

    @staticmethod
    def password_matches(password, stored):
        return verify_password(password, stored)

    def setup_or_login(self):
        """First-run administrator setup or login. Returns True once ``self.current_user`` is set."""
        with get_connection() as con:
            count = con.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        public = ("id", "username", "display_name", "role")

        if count == 0:
            def create_admin(username, display_name, password):
                with get_connection() as con:
                    return create_first_admin(con, username=username, display_name=display_name, password=password)
            dialog = FirstRunDialog(create_admin, self)
        else:
            def check(username, password):
                with get_connection() as con:
                    return authenticate(con, username, password)
            dialog = LoginDialog(check, self)
        accepted = dialog.exec() == QDialog.DialogCode.Accepted and dialog.user is not None
        if not accepted:
            self.current_user = None
            return False
        self.current_user = {k: dialog.user[k] for k in public}   # session = stable DB id + role snapshot (re-checked per action)
        dialog.user = None
        return True

    def require_admin(self):
        """Revalidate the session against SQLite for every privileged action."""
        if not is_admin(self.current_user):
            QMessageBox.warning(self, "صلاحية غير كافية", "الوظيفة دي متاحة لحساب المدير فقط.")
            return False
        try:
            with get_connection() as con:
                row = con.execute("SELECT role,active FROM users WHERE id=?", (self.current_user["id"],)).fetchone()
            if row and row[0] == "admin" and row[1]:
                return True
        except Exception:
            self.logger.exception("Administrator session revalidation failed")
        self.current_user = None
        QMessageBox.warning(self, "انتهت صلاحية الجلسة", "حساب المدير لم يعد نشطًا. سجّل الدخول مرة أخرى.")
        return False

    def reauthenticate_admin(self):
        """Ask for the current admin password before destructive restore operations."""
        if not self.require_admin():
            return False
        password, accepted = QInputDialog.getText(
            self, "تأكيد هوية المدير", "أدخل كلمة مرور المدير للمتابعة:",
            QLineEdit.EchoMode.Password
        )
        if not accepted:
            return False
        with get_connection() as con:
            row = con.execute(
                "SELECT password_hash,active,role FROM users WHERE id=?",
                (self.current_user["id"],)
            ).fetchone()
        if not row or not row[1] or row[2] != "admin" or not self.password_matches(password, row[0]):
            QMessageBox.warning(self, "فشل التحقق", "كلمة مرور المدير غير صحيحة.")
            return None
        return password

    def audit(self, action, entity_type, entity_id="", details=None):
        try:
            with get_connection() as con:
                write_audit_log(con, user_id=(self.current_user or {}).get("id"),
                    username=(self.current_user or {}).get("username", ""),
                    action=action, entity_type=entity_type, entity_id=entity_id,
                    details=details or {})
        except Exception:
            self.logger.exception("Could not write audit event: %s", action)

    def apply_role_permissions(self):
        admin_active = is_admin(self.current_user)
        for i, (_icon, title, _callback) in enumerate(self.nav_items):
            if title not in ("الرئيسية", "الكاشير", "الورديات والخزنة"):
                self.nav_buttons[i].setVisible(admin_active)
        self.backup_button.setVisible(admin_active)
        self.restore_button.setVisible(admin_active)
        self.sidebar_toggle.setVisible(admin_active)
        if admin_active:
            self.discount_input.setEnabled(True)
        else:
            self.discount_input.setValue(0); self.discount_input.setEnabled(False)
            self.sidebar_toggle.setVisible(False)
        self.user_label.setText(f"{self.current_user['display_name']}\n{'مدير النظام' if admin_active else 'كاشير'}")
        if hasattr(self, "topbar_user_label"):
            self.topbar_user_label.setText(self.current_user["display_name"])

    def build_interface(self):
        central = QWidget()
        central.setObjectName("AppShell")
        self.setCentralWidget(central)
        main_layout = QHBoxLayout(central)
        main_layout.setContentsMargins(0, 0, 0, 0)
        main_layout.setSpacing(0)

        sidebar = QFrame()
        sidebar.setObjectName("Sidebar")
        sidebar.setFixedWidth(244)
        self.sidebar = sidebar
        self.sidebar_collapsed = False
        side = QVBoxLayout(sidebar)
        side.setContentsMargins(14, 16, 14, 16)
        side.setSpacing(5)
        self.sidebar_layout = side

        self.sidebar_toggle = QPushButton("☰   تصغير القائمة")
        self.sidebar_toggle.setObjectName("NavToggle")
        self.sidebar_toggle.clicked.connect(self.toggle_sidebar)
        side.addWidget(self.sidebar_toggle)

        brand = QLabel("SmartPOS")
        brand.setObjectName("Brand")
        brand.setAlignment(Qt.AlignmentFlag.AlignCenter)
        side.addWidget(brand)
        self.brand_label = brand

        subtitle = QLabel("RETAIL MANAGEMENT SYSTEM")
        subtitle.setObjectName("SidebarSubtitle")
        subtitle.setAlignment(Qt.AlignmentFlag.AlignCenter)
        side.addWidget(subtitle)
        self.sidebar_subtitle = subtitle

        separator = QFrame()
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setObjectName("SidebarSeparator")
        side.addWidget(separator)

        self.pages = QStackedWidget()
        navigation = [
            ("⌂", "الرئيسية", self.show_home),
            ("▣", "الكاشير", self.show_cashier),
            ("◷", "الورديات والخزنة", self.show_shifts),
            ("▦", "المنتجات والمخزون", self.show_products),
            ("▤", "الفواتير", self.show_invoices),
            ("▧", "المشتريات", self.show_purchases),
            ("♧", "الموردون", self.show_suppliers),
            ("▥", "التقارير", self.show_reports),
            ("♙", "المستخدمون", self.show_users),
            ("♧", "العملاء والمديونيات", self.show_customers),
        ]
        self.nav_buttons = []
        self.nav_items = navigation
        for icon, title, callback in navigation:
            button = QPushButton(f"{icon}    {title}")
            button.setToolTip(title)
            button.setObjectName("NavButton")
            button.setCheckable(True)
            button.setMinimumHeight(42)
            button.clicked.connect(callback)
            side.addWidget(button)
            self.nav_buttons.append(button)

        side.addStretch(1)
        self.user_label = QLabel("")
        self.user_label.setObjectName("SidebarUser")
        self.user_label.setWordWrap(True)
        side.addWidget(self.user_label)

        backup_btn = QPushButton("▣    النسخ الاحتياطي")
        backup_btn.setObjectName("NavToggle")
        backup_btn.setToolTip("النسخ الاحتياطي")
        backup_btn.setMinimumHeight(39)
        backup_btn.clicked.connect(self.open_backup_options)
        side.addWidget(backup_btn)
        self.backup_button = backup_btn

        restore_btn = QPushButton("↶    استعادة نسخة احتياطية")
        restore_btn.setObjectName("NavToggle")
        restore_btn.setMinimumHeight(39)
        restore_btn.clicked.connect(self.restore_backup)
        side.addWidget(restore_btn)
        self.restore_button = restore_btn

        self.backup_status_label = QLabel("حالة النسخ الاحتياطي: جاري التحقق")
        self.backup_status_label.setObjectName("SidebarVersion")
        self.backup_status_label.setWordWrap(True)
        side.addWidget(self.backup_status_label)

        logout_btn = QPushButton("⇥    تسجيل الخروج")
        logout_btn.setObjectName("NavToggle")
        logout_btn.setToolTip("تسجيل الخروج")
        logout_btn.setMinimumHeight(39)
        logout_btn.clicked.connect(self.logout)
        side.addWidget(logout_btn)
        self.logout_button = logout_btn

        version = QLabel(f"SMARTPOS  •  VERSION {APP_VERSION}")
        version.setObjectName("SidebarVersion")
        version.setAlignment(Qt.AlignmentFlag.AlignCenter)
        side.addWidget(version)
        self.sidebar_version = version

        content = QWidget()
        content.setObjectName("MainContent")
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(25, 18, 25, 22)
        content_layout.setSpacing(13)

        workspace_header = QFrame()
        workspace_header.setObjectName("WorkspaceHeader")
        workspace_header.setFixedHeight(58)
        header_layout = QHBoxLayout(workspace_header)
        header_layout.setContentsMargins(16, 8, 16, 8)
        header_layout.setSpacing(10)

        header_identity = QVBoxLayout()
        header_identity.setSpacing(1)
        eyebrow = QLabel("SMARTPOS  /  WORKSPACE")
        eyebrow.setObjectName("WorkspaceEyebrow")
        workspace_title = QLabel("إدارة المتجر")
        workspace_title.setObjectName("WorkspaceTitle")
        header_identity.addWidget(eyebrow)
        header_identity.addWidget(workspace_title)
        header_layout.addLayout(header_identity)
        header_layout.addStretch(1)

        status = QLabel("●   نظام نقاط البيع")
        status.setObjectName("SystemStatus")
        header_layout.addWidget(status)
        self.topbar_user_label = QLabel(self.current_user.get("display_name", "") if self.current_user else "")
        self.topbar_user_label.setObjectName("WorkspaceUser")
        header_layout.addWidget(self.topbar_user_label)
        content_layout.addWidget(workspace_header)
        content_layout.addWidget(self.pages, 1)

        main_layout.addWidget(sidebar)
        main_layout.addWidget(content, 1)

        self.home_page = self.create_home_page()
        self.products_page = self.create_products_page()
        self.cashier_page = self.create_cashier_page()
        self.invoices_page = self.create_invoices_page()
        self.reports_page = self.create_reports_page()
        self.purchases_page = self.create_purchases_page()
        self.suppliers_page = self.create_suppliers_page()
        self.users_page = self.create_users_page()
        self.shifts_page = self.create_shifts_page()
        self.customers_page = self.create_customers_page()
        for page in (self.home_page, self.cashier_page, self.shifts_page, self.products_page,
                     self.invoices_page, self.purchases_page, self.suppliers_page,
                     self.reports_page, self.users_page, self.customers_page):
            self.pages.addWidget(page)

    def setup_backup_system(self):
        """Use per-user configuration and expose backup health in the shell."""
        self.backup_settings_path = CONFIG_DIR / "backup_settings.json"
        self.backup_folder = str(BACKUP_DIR)
        setup_warning = ""
        try:
            if self.backup_settings_path.exists():
                settings = json.loads(self.backup_settings_path.read_text(encoding="utf-8"))
                configured = settings.get("backup_folder")
                if configured and Path(configured).expanduser().is_absolute():
                    self.backup_folder = str(Path(configured).expanduser())
            target = Path(self.backup_folder).expanduser().resolve()
            target.mkdir(parents=True, exist_ok=True)
            probe = target / ".smartpos_write_test"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink(missing_ok=True)
            self.backup_folder = str(target)
        except Exception:
            self.logger.exception("Configured backup folder is unavailable; falling back to application data")
            setup_warning = "تعذر استخدام مجلد النسخ المحدد؛ تم الرجوع للمجلد الافتراضي."
            self.backup_folder = str(BACKUP_DIR)
            Path(self.backup_folder).mkdir(parents=True, exist_ok=True)

        latest = None
        try:
            backups = sorted(Path(self.backup_folder).glob("SmartPOS_backup_*.db"),
                             key=lambda item: item.stat().st_mtime, reverse=True)
            latest = backups[0] if backups else None
        except OSError:
            self.logger.exception("Could not inspect backup history")
        if hasattr(self, "backup_status_label"):
            if latest:
                stamp = datetime.fromtimestamp(latest.stat().st_mtime).strftime("%Y-%m-%d %H:%M")
                stale = datetime.now() - datetime.fromtimestamp(latest.stat().st_mtime) > timedelta(hours=24)
                stale_warning = "تحذير: آخر نسخة احتياطية أقدم من 24 ساعة" if stale else ""
                self.backup_status_label.setText(f"آخر نسخة: {stamp}" + "".join(f"\n{w}" for w in (stale_warning, setup_warning) if w))
            else:
                self.backup_status_label.setText("لم تُنشأ نسخة احتياطية بعد" + (f"\n{setup_warning}" if setup_warning else ""))

        self.backup_timer = QTimer(self)
        self.backup_timer.setInterval(10 * 60 * 1000)
        self.backup_timer.timeout.connect(self.run_automatic_backup)
        self.backup_timer.start()

    def open_backup_options(self):
        if not self.require_admin():
            return
        folder = QFileDialog.getExistingDirectory(
            self, "اختار المجلد المخصص لحفظ النسخ الاحتياطية", self.backup_folder or str(BACKUP_DIR)
        )
        if not folder:
            return
        try:
            target = Path(folder).expanduser().resolve()
            target.mkdir(parents=True, exist_ok=True)
            test_path = target / ".smartpos_write_test"
            test_path.write_text("ok", encoding="utf-8")
            test_path.unlink(missing_ok=True)
            self.backup_folder = str(target)
            self.backup_settings_path.parent.mkdir(parents=True, exist_ok=True)
            self.backup_settings_path.write_text(
                json.dumps({"backup_folder": self.backup_folder}, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            if not self.create_backup(show_message=False):
                raise RuntimeError("لم يتم إنشاء النسخة التجريبية")
            self.audit("backup_location_changed", "settings", "backup_folder", {"path": self.backup_folder})
            QMessageBox.information(
                self, "النسخ الاحتياطي", "تم تحديد المجلد وإنشاء نسخة احتياطية والتحقق منها.\n"
                "سيتم إنشاء نسخة تلقائيًا كل 10 دقائق مع الاحتفاظ بآخر 30 نسخة."
            )
        except Exception as exc:
            self.logger.exception("Backup destination setup failed")
            QMessageBox.critical(self, "خطأ في النسخ الاحتياطي", f"تعذر استخدام المجلد المحدد:\n{exc}")

    def restore_backup(self):
        """Perform an explicit, authenticated, validated in-place database restore."""
        admin_password = self.reauthenticate_admin()
        if not admin_password:
            return
        source_name, _ = QFileDialog.getOpenFileName(
            self, "اختيار النسخة الاحتياطية للاستعادة", str(Path(self.backup_folder)),
            "SmartPOS Database (*.db);;All files (*.*)"
        )
        if not source_name:
            return
        source_path = Path(source_name).expanduser().resolve()
        try:
            if source_path == DATABASE_PATH.resolve():
                raise ValueError("لا يمكن اختيار قاعدة البيانات الحالية نفسها.")
            validate_database_file(source_path)
            answer = QMessageBox.question(
                self, "تأكيد استعادة البيانات",
                "سيتم استبدال بيانات التشغيل بالنسخة المختارة الآن.\n"
                "سيتم إنشاء نسخة أمان مستقلة من قاعدة البيانات الحالية أولًا.\n\nهل أنت متأكد؟",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No
            )
            if answer != QMessageBox.StandardButton.Yes:
                return
            safety_path = restore_database_file(source_path, DATABASE_PATH,
                MIGRATION_BACKUPS_DIR, self.current_user, admin_password=admin_password)
            initialize_database()
            self.logger.warning("Database restored from %s; safety copy: %s", source_path, safety_path)
            # The restored DB may contain a different admin roster. Never retain a
            # session whose identity no longer exists in the restored database.
            with get_connection() as con:
                user_row = con.execute(
                    "SELECT id,username,display_name,role,active FROM users WHERE id=?",
                    (self.current_user["id"],)
                ).fetchone()
            if not user_row or not user_row[4] or user_row[3] != "admin":
                QMessageBox.information(self, "تمت الاستعادة", "اكتملت الاستعادة. سيتم إغلاق البرنامج لإعادة تسجيل الدخول بالبيانات المستعادة.")
                QApplication.quit()
                return
            self.current_user = {"id":user_row[0],"username":user_row[1],"display_name":user_row[2],"role":user_row[3]}
            QMessageBox.information(self, "تمت الاستعادة", f"اكتملت الاستعادة بنجاح.\nنسخة الأمان: {safety_path}\nسيتم تحديث الشاشات الآن.")
            self.refresh_home_dashboard()
            self.load_products(); self.load_invoices(); self.load_reports(); self.load_purchases(); self.load_suppliers(); self.load_users(); self.refresh_shifts_page(); self.load_customers()
            self.refresh_cashier_products()
        except Exception as exc:
            self.logger.exception("Database restore failed")
            QMessageBox.critical(self, "تعذر استعادة البيانات", f"لم تكتمل الاستعادة:\n{exc}\n\nتم الاحتفاظ بنسخة الأمان إذا كانت قد أُنشئت.")

    def create_backup(self, show_message=True):
        if show_message and not self.require_admin():
            return False
        try:
            target = create_database_backup(DATABASE_PATH, self.backup_folder or BACKUP_DIR, tiered=True)
            if hasattr(self, "backup_status_label"):
                self.backup_status_label.setText(f"آخر نسخة: {datetime.now():%Y-%m-%d %H:%M}")
            if show_message:
                self.audit("database_backup_created", "database", str(target))
                QMessageBox.information(self, "تم النسخ الاحتياطي", f"تم حفظ نسخة سليمة:\n{target}")
            return True
        except Exception as exc:
            self.logger.exception("SmartPOS backup failed")
            if show_message:
                QMessageBox.critical(self, "فشل النسخ الاحتياطي", f"تعذر إنشاء النسخة:\n{exc}")
            return False

    def run_automatic_backup(self):
        if not self.backup_folder:
            return
        running = getattr(self, "_backup_thread", None)
        if running is not None and running.isRunning():
            return                                   # previous backup still running: skip this tick
        self._backup_thread = AutoBackupThread(DATABASE_PATH, self.backup_folder, self)
        self._backup_thread.finished_with.connect(self.on_automatic_backup_finished)
        self._backup_thread.start()

    def on_automatic_backup_finished(self, path, error):
        """Runs on the GUI thread (queued signal) when the background backup ends."""
        if error:
            self.logger.error("Automatic backup failed: %s", error)
            if hasattr(self, "backup_status_label"):
                self.backup_status_label.setText("تحذير: فشل النسخ الاحتياطي — راجع السجل")
            return
        self.logger.info("Automatic backup completed: %s", path)
        if hasattr(self, "backup_status_label"):
            self.backup_status_label.setText(f"آخر نسخة: {datetime.now():%Y-%m-%d %H:%M}")

    def closeEvent(self, event):
        running = getattr(self, "_backup_thread", None)
        if running is not None and running.isRunning():
            running.wait(15000)                      # never destroy the window while a backup thread is writing
        if is_admin(self.current_user):
            answer = QMessageBox.question(
                self, "إغلاق Smart POS", "هل تريد إنشاء نسخة احتياطية الآن قبل إغلاق البرنامج؟",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.No
            )
            if answer == QMessageBox.StandardButton.Cancel:
                event.ignore(); return
            if answer == QMessageBox.StandardButton.Yes and not self.create_backup(show_message=True):
                event.ignore(); return
        else:
            answer = QMessageBox.question(self, "إغلاق Smart POS", "هل تريد إغلاق البرنامج؟",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No)
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore(); return
        if hasattr(self, "backup_timer"):
            self.backup_timer.stop()
        event.accept()

    def toggle_sidebar(self):
        self.sidebar_collapsed = not self.sidebar_collapsed
        self.sidebar.setFixedWidth(72 if self.sidebar_collapsed else 244)
        self.sidebar_toggle.setText("☰" if self.sidebar_collapsed else "☰  تصغير القائمة")
        for button, (icon, title, _callback) in zip(self.nav_buttons, self.nav_items):
            button.setText(icon if self.sidebar_collapsed else f"{icon}   {title}")
            button.setToolTip(title)
            button.setStyleSheet("text-align:center; padding:11px 4px;" if self.sidebar_collapsed else "")
        self.backup_button.setText("▣" if self.sidebar_collapsed else "▣    النسخ الاحتياطي")
        self.restore_button.setText("↶" if self.sidebar_collapsed else "↶    استعادة نسخة احتياطية")
        self.logout_button.setText("⇥" if self.sidebar_collapsed else "⇥    تسجيل الخروج")
        footer_style = "text-align:center; padding:10px 4px;" if self.sidebar_collapsed else "text-align:right;"
        self.backup_button.setStyleSheet(footer_style)
        self.restore_button.setStyleSheet(footer_style)
        self.logout_button.setStyleSheet(footer_style)
        self.brand_label.setVisible(not self.sidebar_collapsed)
        self.sidebar_subtitle.setVisible(not self.sidebar_collapsed)
        self.sidebar_version.setVisible(not self.sidebar_collapsed)

    def set_active_button(self, index):
        for i, button in enumerate(self.nav_buttons): button.setChecked(i == index)

    def show_home(self):
        self.pages.setCurrentWidget(self.home_page)
        self.set_active_button(0)
        self.refresh_home_dashboard()

    def refresh_home_dashboard(self):
        """Refresh dashboard metrics for the 04:00-to-04:00 business day."""
        if not hasattr(self, "dashboard_sales"):
            return

        now = datetime.now()
        business_date = datetime.strptime(business_date_for(now), "%Y-%m-%d").date()
        day_start = datetime.combine(business_date, datetime.min.time()).replace(hour=4)
        day_end = day_start + timedelta(days=1)

        try:
            with get_connection() as con:
                invoice_count, gross_sales = con.execute(
                    """
                    SELECT COUNT(*), COALESCE(SUM(total), 0)
                    FROM invoices
                    WHERE business_date = ?
                    """,
                    (business_date.isoformat(),),
                ).fetchone()

                returns_total = con.execute(
                    """
                    SELECT COALESCE(SUM(total_refund), 0)
                    FROM sales_returns
                    WHERE business_date = ?
                    """,
                    (business_date.isoformat(),),
                ).fetchone()[0]

                low_stock = con.execute(
                    """
                    SELECT code, name, stock_quantity
                    FROM products
                    WHERE stock_quantity <= min_stock
                    ORDER BY stock_quantity ASC, name COLLATE NOCASE ASC
                    LIMIT 20
                    """
                ).fetchall()

                recent_invoices = con.execute(
                    """
                    SELECT COALESCE(daily_number, invoice_number), created_at,
                           total, payment_method
                    FROM invoices
                    ORDER BY id DESC
                    LIMIT 8
                    """
                ).fetchall()

            # Amounts are stored as piasters throughout this application.
            net_sales = gross_sales - returns_total
            self.dashboard_sales.setText(money(gross_sales))
            self.dashboard_invoice_count.setText(str(invoice_count))
            self.dashboard_returns.setText(money(returns_total))
            self.dashboard_net_sales.setText(money(net_sales))
            self.dashboard_period.setText(
                f"اليوم التجاري: {business_date.isoformat()} — "
                f"من {day_start.strftime('%H:%M')} إلى {day_end.strftime('%Y-%m-%d %H:%M')}"
            )

            self.dashboard_stock_table.setRowCount(len(low_stock))
            for row_index, row in enumerate(low_stock):
                for col_index, value in enumerate(row):
                    item = QTableWidgetItem(str(value if value is not None else ""))
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    self.dashboard_stock_table.setItem(row_index, col_index, item)

            self.dashboard_recent_table.setRowCount(len(recent_invoices))
            for row_index, row in enumerate(recent_invoices):
                invoice_number, created_at, total, payment_method = row
                values = [invoice_number, created_at, money(total), payment_method]
                for col_index, value in enumerate(values):
                    item = QTableWidgetItem(str(value if value is not None else ""))
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    self.dashboard_recent_table.setItem(row_index, col_index, item)

        except Exception as error:
            QMessageBox.warning(
                self,
                "خطأ في لوحة التحكم",
                f"تعذر تحديث بيانات لوحة التحكم:\n{error}",
            )

    def show_cashier(self):
        self.pages.setCurrentWidget(self.cashier_page); self.set_active_button(1); self.refresh_customer_combo(); self.refresh_cashier_products()
        try:
            shift = self.get_open_shift()
            self.cashier_shift_button.setText("✓ الوردية مفتوحة — إدارة الخزنة" if shift else "🕒 افتح ورديتك قبل البيع")
            self.cashier_shift_button.setStyleSheet(T.qss("background:@success@;color:@text_on_deep@;border-radius:8px;padding:10px;font-weight:bold;") if shift else T.qss("background:@accent_alt@;color:@text_on_accent@;border-radius:8px;padding:10px;font-weight:bold;"))
        except Exception:
            pass
    def show_shifts(self):
        self.pages.setCurrentWidget(self.shifts_page); self.set_active_button(2); self.refresh_shifts_page()
    def show_products(self):
        if not self.require_admin(): return
        self.pages.setCurrentWidget(self.products_page); self.set_active_button(3); self.load_products()
    def show_invoices(self):
        if not self.require_admin(): return
        self.pages.setCurrentWidget(self.invoices_page); self.set_active_button(4); self.load_invoices()
    def show_reports(self):
        if not self.require_admin(): return
        self.pages.setCurrentWidget(self.reports_page); self.set_active_button(7); self.load_reports()
    def show_purchases(self):
        if not self.require_admin(): return
        self.pages.setCurrentWidget(self.purchases_page); self.set_active_button(5); self.load_purchases()
    def show_suppliers(self):
        if not self.require_admin(): return
        self.pages.setCurrentWidget(self.suppliers_page); self.set_active_button(6); self.load_suppliers()
    def show_users(self):
        if not self.require_admin(): return
        self.pages.setCurrentWidget(self.users_page); self.set_active_button(8); self.load_users()
    def show_customers(self):
        if not self.require_admin(): return
        self.pages.setCurrentWidget(self.customers_page); self.set_active_button(9); self.load_customers()

    def create_customers_page(self):
        page = QWidget(); layout = QVBoxLayout(page); layout.setSpacing(12)
        header = QHBoxLayout(); title = QLabel("العملاء والمديونيات"); title.setObjectName("PageTitle")
        header.addWidget(title); header.addStretch(1)
        add_btn = QPushButton("＋ إضافة عميل"); add_btn.setObjectName("Primary"); add_btn.clicked.connect(self.add_customer_dialog); header.addWidget(add_btn)
        collect_btn = QPushButton("تحصيل مديونية نقدًا"); collect_btn.setObjectName("Primary"); collect_btn.clicked.connect(self.collect_customer_dialog); header.addWidget(collect_btn)
        refresh_btn = QPushButton("تحديث"); refresh_btn.clicked.connect(self.load_customers); header.addWidget(refresh_btn)
        layout.addLayout(header)
        note = QLabel("الرصيد الموجب يعني مبلغًا مستحقًا على العميل. كل بيع آجل أو تحصيل أو مرتجع على حساب العميل يُسجل في دفتر حركات مستقل.")
        note.setObjectName("Muted"); note.setWordWrap(True); layout.addWidget(note)
        self.customers_table = QTableWidget(0,5)
        self.customers_table.setHorizontalHeaderLabels(["ID","اسم العميل","رقم الهاتف","الرصيد المستحق","الحالة"])
        self.customers_table.setColumnHidden(0,True); self.customers_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.customers_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.customers_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.customers_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.customers_table.verticalHeader().setVisible(False); layout.addWidget(self.customers_table,1)
        self.customers_status = QLabel(""); self.customers_status.setObjectName("Muted"); layout.addWidget(self.customers_status)
        return page

    def selected_customer_id(self):
        row = self.customers_table.currentRow() if hasattr(self,"customers_table") else -1
        if row < 0 or not self.customers_table.item(row,0):
            return None
        return int(self.customers_table.item(row,0).text())

    def load_customers(self):
        if not self.require_admin(): return
        if not hasattr(self,"customers_table"):
            return
        with get_connection() as con:
            rows = con.execute("""
                SELECT c.id,c.name,c.phone,COALESCE(SUM(l.debit-l.credit),0),c.active
                FROM customers c LEFT JOIN customer_ledger l ON l.customer_id=c.id
                GROUP BY c.id ORDER BY c.name COLLATE NOCASE
            """).fetchall()
        self.customers_table.setRowCount(len(rows))
        for r,row in enumerate(rows):
            values=[row[0],row[1],row[2] or "—",money(int(row[3])),"نشط" if row[4] else "موقوف"]
            for c,value in enumerate(values):
                cell=QTableWidgetItem(str(value)); cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if c==3:
                    cell.setForeground(Qt.GlobalColor.red if int(row[3])>0 else Qt.GlobalColor.darkGreen)
                self.customers_table.setItem(r,c,cell)
        self.customers_status.setText(f"عدد العملاء: {len(rows)} — الأرصدة مستمدة من دفتر الحركات، وليست قيمة ثابتة قابلة للتعديل.")
        self.refresh_customer_combo()

    def refresh_customer_combo(self):
        if not hasattr(self,"customer_combo"):
            return
        current = self.customer_combo.currentText().strip()
        self.customer_combo.blockSignals(True)
        self.customer_combo.clear()
        self.customer_combo.addItem("", None)
        try:
            with get_connection() as con:
                rows=con.execute("SELECT id,name,phone FROM customers WHERE active=1 ORDER BY name COLLATE NOCASE,id").fetchall()
            # Duplicate names get a phone / #id suffix so every entry is unique and a typed label can
            # only ever match ONE customer (identity is the id, never the name).
            labels=build_customer_labels([(int(r[0]),r[1],r[2] or "") for r in rows])
            for (customer_id,_name,_phone),label in zip(rows,labels):
                self.customer_combo.addItem(label,int(customer_id))
        except Exception:
            self.logger.exception("Unable to refresh customer selector")
        self.customer_combo.setEditable(True)
        self.customer_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.customer_combo.setCurrentText(current if current else "")
        self.customer_combo.setPlaceholderText("اختر العميل (مطلوب عند البيع الآجل)")
        self.customer_combo.blockSignals(False)

    def add_customer_dialog(self):
        if not self.require_admin():
            return
        dialog=QDialog(self); dialog.setWindowTitle("إضافة عميل"); dialog.setMinimumWidth(420); dialog.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        layout=QVBoxLayout(dialog); form=QFormLayout()
        name=QLineEdit(); name.setPlaceholderText("اسم العميل")
        phone=QLineEdit(); phone.setPlaceholderText("رقم الهاتف - اختياري")
        form.addRow("اسم العميل:",name); form.addRow("رقم الهاتف:",phone); layout.addLayout(form)
        buttons=QHBoxLayout(); save=QPushButton("حفظ العميل"); save.setObjectName("Primary"); cancel=QPushButton("إلغاء")
        buttons.addStretch(); buttons.addWidget(cancel); buttons.addWidget(save); layout.addLayout(buttons)
        cancel.clicked.connect(dialog.reject)
        def save_customer():
            value=name.text().strip()
            if not value:
                QMessageBox.warning(dialog,"اسم مطلوب","اكتب اسم العميل."); return
            try:
                allow_duplicate=False
                with get_connection() as con:
                    same=find_customers_by_name(con,value)
                if same:
                    listing="\n".join(f"• {c['name']} — {c['phone'] or 'بدون هاتف'}" for c in same)
                    answer=QMessageBox.question(dialog,"اسم مكرر",f"يوجد عميل مسجل بنفس الاسم:\n{listing}\n\nهل تريد فعلًا إنشاء عميل جديد بنفس الاسم؟",
                        QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)
                    if answer!=QMessageBox.StandardButton.Yes: return
                    allow_duplicate=True
                with get_connection() as con:
                    customer_id=create_customer(con,value,phone.text(),allow_duplicate=allow_duplicate)
                    write_audit_log(con,user_id=self.current_user["id"],username=self.current_user["username"],
                        action="customer_created",entity_type="customer",entity_id=customer_id)
                dialog.accept(); self.load_customers()
            except Exception as exc:
                QMessageBox.critical(dialog,"تعذر حفظ العميل",str(exc))
        save.clicked.connect(save_customer); dialog.exec()

    def collect_customer_dialog(self):
        if not self.require_admin():
            return
        customer_id=self.selected_customer_id()
        if customer_id is None:
            QMessageBox.information(self,"اختيار عميل","اختر العميل المراد تحصيل مديونيته أولًا."); return
        shift=self.get_open_shift()
        if not shift:
            QMessageBox.warning(self,"لا توجد وردية","افتح وردية للمدير أولًا حتى يُسجل التحصيل داخل الخزنة."); return
        with get_connection() as con:
            row=con.execute("SELECT name FROM customers WHERE id=?",(customer_id,)).fetchone()
            balance=customer_balance(con,customer_id)
        if not row or balance<=0:
            QMessageBox.information(self,"لا توجد مديونية","هذا العميل ليس عليه رصيد مستحق."); return
        amount,ok=QInputDialog.getDouble(self,"تحصيل مديونية",f"العميل: {row[0]}\nالرصيد المستحق: {money(balance)}\nالمبلغ المحصل نقدًا بالجنيه:",balance/100,0.01,balance/100,2)
        if not ok:
            return
        notes,ok=QInputDialog.getText(self,"ملاحظات التحصيل","ملاحظة اختيارية:")
        if not ok:
            return
        try:
            with get_connection() as con:
                con.execute("BEGIN IMMEDIATE")
                collect_customer_payment(con,customer_id=customer_id,amount=to_piasters(amount),
                    user_id=self.current_user["id"],shift_id=shift[0],notes=notes or "")
                write_audit_log(con,user_id=self.current_user["id"],username=self.current_user["username"],
                    action="customer_payment_collected",entity_type="customer",entity_id=customer_id,
                    details={"amount":to_piasters(amount)})
            QMessageBox.information(self,"تم التحصيل",f"تم تسجيل تحصيل {money(to_piasters(amount))} نقدًا.")
            self.load_customers(); self.refresh_shifts_page()
        except Exception as exc:
            QMessageBox.critical(self,"تعذر التحصيل",str(exc))

    def reset_session_ui(self):
        """Drop everything that belongs to the previous user's session (cart, discount, payment entry)."""
        self.cart.clear()
        self.discount_input.setEnabled(True)
        self.discount_input.setValue(0)
        self.paid_input.setValue(0)
        self.render_cart()

    def logout(self):
        answer = QMessageBox.question(self, "تسجيل الخروج", "هل تريد تسجيل الخروج والعودة لشاشة الدخول؟")
        if answer != QMessageBox.StandardButton.Yes: return
        previous = self.current_user
        self.current_user = None                 # permissions are gone before anything else happens
        self.reset_session_ui()
        self.hide()                              # the previous user's screens must not stay visible behind the login box
        if previous:
            self.audit_logout(previous)
        if self.setup_or_login():
            self.apply_role_permissions()
            self.show()
            self.show_cashier() if self.current_user["role"] == "cashier" else self.show_home()
        else:
            QApplication.quit()

    def audit_logout(self, user):
        try:
            with get_connection() as con:
                write_audit_log(con, user_id=user.get("id"), username=user.get("username", ""),
                                action="logout", entity_type="user", entity_id=user.get("id"), details={})
        except Exception:
            self.logger.exception("Could not write logout audit event")

    def create_users_page(self):
        page = QWidget(); layout = QVBoxLayout(page)
        header = QHBoxLayout(); title = QLabel("إدارة المستخدمين"); title.setObjectName("PageTitle"); header.addWidget(title); header.addStretch()
        add_btn = QPushButton("＋ إضافة كاشير"); add_btn.setObjectName("Primary"); add_btn.clicked.connect(self.add_cashier_user); header.addWidget(add_btn)
        reset_btn = QPushButton("إعادة تعيين كلمة المرور"); reset_btn.clicked.connect(self.reset_user_password); header.addWidget(reset_btn)
        toggle_btn = QPushButton("⏻ إيقاف / تفعيل الحساب"); toggle_btn.clicked.connect(self.toggle_user_active); header.addWidget(toggle_btn)
        delete_btn = QPushButton("🗑 حذف الكاشير"); delete_btn.setObjectName("Danger"); delete_btn.clicked.connect(self.delete_cashier_user); header.addWidget(delete_btn)
        refresh_btn = QPushButton("تحديث"); refresh_btn.clicked.connect(self.load_users); header.addWidget(refresh_btn); layout.addLayout(header)
        note = QLabel("حساب المدير يملك جميع الصلاحيات. حساب الكاشير مخصص للبيع فقط، ولا يمكنه فتح صفحات الإدارة أو التقارير."); note.setObjectName("Muted"); note.setWordWrap(True); layout.addWidget(note)
        self.users_table = QTableWidget(0, 5); self.users_table.setHorizontalHeaderLabels(["ID", "اسم المستخدم", "الاسم الظاهر", "الدور", "الحالة"]); self.users_table.setColumnHidden(0, True); self.users_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers); self.users_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows); self.users_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection); self.users_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch); self.users_table.verticalHeader().setVisible(False); layout.addWidget(self.users_table, 1)
        self.users_status = QLabel(""); self.users_status.setObjectName("Muted"); layout.addWidget(self.users_status)
        return page

    def load_users(self):
        if not self.require_admin(): return
        if not hasattr(self, "users_table"): return
        selected_id = self.selected_user() if self.users_table.currentRow() >= 0 else None
        with get_connection() as con:
            rows = con.execute("SELECT id,username,display_name,role,active FROM users ORDER BY role,username").fetchall()
        self.users_table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            vals = [str(row[0]), row[1], row[2], "مدير" if row[3] == "admin" else "كاشير", "مفعّل" if row[4] else "موقوف"]
            for c, val in enumerate(vals):
                item = QTableWidgetItem(val); item.setTextAlignment(Qt.AlignmentFlag.AlignCenter); self.users_table.setItem(r,c,item)
                if c == 4:
                    item.setForeground(Qt.GlobalColor.darkGreen if row[4] else Qt.GlobalColor.red)
            if selected_id is not None and int(row[0]) == selected_id:
                self.users_table.selectRow(r)
        self.users_table.setStyleSheet(T.qss("QTableWidget::item:selected { background:@primary@; color:@text_on_primary@; }"))
        self.users_status.setText(f"عدد الحسابات: {len(rows)} — اختر صفًا ثم استخدم الأزرار بالأعلى.")

    def delete_cashier_user(self):
        """Deactivate a cashier instead of deleting an identity used in audit history."""
        if not self.require_admin():
            return
        user_id = self.selected_user()
        if user_id is None:
            QMessageBox.information(self, "اختيار حساب", "اختار حساب كاشير من الجدول أولًا.")
            return
        with get_connection() as con:
            row = con.execute(
                "SELECT username,display_name,role,active FROM users WHERE id=?", (user_id,)
            ).fetchone()
            if not row:
                return
            if row[2] != "cashier":
                QMessageBox.warning(self, "غير مسموح", "يمكن إيقاف حسابات الكاشير فقط من هذه الشاشة.")
                return
            if not row[3]:
                QMessageBox.information(self, "الحساب موقوف", "هذا الحساب موقوف بالفعل.")
                return
            open_shift = con.execute(
                "SELECT id FROM cash_shifts WHERE user_id=? AND status='open' LIMIT 1", (user_id,)
            ).fetchone()
        if open_shift:
            QMessageBox.warning(self, "وردية مفتوحة", "لا يمكن إيقاف الحساب قبل إغلاق الوردية المفتوحة الخاصة به.")
            return
        answer = QMessageBox.question(
            self, "تأكيد إيقاف الكاشير",
            f"هل تريد إيقاف حساب «{row[1]}» ({row[0]})؟\n\nلن يتم حذف الفواتير أو الورديات أو السجلات التاريخية.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            with get_connection() as con:
                set_user_active(con, actor=self.current_user, target_user_id=user_id, active=False)
        except (PermissionError, ValueError) as exc:
            QMessageBox.warning(self, "تعذر إيقاف الحساب", str(exc)); return
        self.load_users()
        QMessageBox.information(self, "تم إيقاف الحساب", "تم إيقاف حساب الكاشير مع الاحتفاظ بجميع سجلاته التاريخية.")

    def selected_user(self):
        row = self.users_table.currentRow()
        if row < 0: return None
        item = self.users_table.item(row, 0)
        return int(item.text()) if item else None

    def add_cashier_user(self):
        if not self.require_admin(): return
        dialog = QDialog(self); dialog.setWindowTitle("إضافة حساب كاشير"); dialog.setMinimumWidth(420); dialog.setLayoutDirection(Qt.LayoutDirection.RightToLeft); layout = QVBoxLayout(dialog); form = QFormLayout()
        username = QLineEdit(); display = QLineEdit(); password = QLineEdit(); password.setEchoMode(QLineEdit.EchoMode.Password); confirm = QLineEdit(); confirm.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("اسم المستخدم:",username); form.addRow("الاسم الظاهر:",display); form.addRow("كلمة المرور (8 أحرف على الأقل):",password); form.addRow("تأكيد كلمة المرور:",confirm); layout.addLayout(form)
        buttons = QHBoxLayout(); save = QPushButton("حفظ الحساب"); save.setObjectName("Primary"); cancel = QPushButton("إلغاء"); buttons.addWidget(save); buttons.addWidget(cancel); layout.addLayout(buttons); cancel.clicked.connect(dialog.reject)
        def save_user():
            u, name, pw = username.text().strip(), display.text().strip(), password.text()
            if not u or not name: QMessageBox.warning(dialog,"بيانات ناقصة","اكتب اسم المستخدم والاسم الظاهر."); return
            if pw != confirm.text(): QMessageBox.warning(dialog,"عدم تطابق","كلمتا المرور غير متطابقتين."); return
            try:
                with get_connection() as con: create_user(con, actor=self.current_user, username=u, display_name=name, password=pw, role="cashier")
                dialog.accept(); self.load_users(); QMessageBox.information(self,"تم الحفظ","تم إنشاء حساب الكاشير بنجاح.")
            except (ValueError, PermissionError) as exc: QMessageBox.warning(dialog,"تعذر الحفظ",str(exc))
            except sqlite3.IntegrityError: QMessageBox.warning(dialog,"اسم مستخدم موجود","اختار اسم مستخدم مختلف.")
        save.clicked.connect(save_user); dialog.exec()

    def reset_user_password(self):
        if not self.require_admin(): return
        user_id = self.selected_user()
        if user_id is None: QMessageBox.information(self,"اختيار حساب","اختار حسابًا من الجدول أولًا."); return
        with get_connection() as con: row = con.execute("SELECT username,role FROM users WHERE id=?",(user_id,)).fetchone()
        if not row: return
        password, ok = QInputDialog.getText(self,"إعادة تعيين كلمة المرور",f"كلمة المرور الجديدة للحساب {row[0]} (8 أحرف على الأقل):",QLineEdit.EchoMode.Password)
        if not ok: return
        try:
            with get_connection() as con: service_reset_password(con, actor=self.current_user, target_user_id=user_id, new_password=password)
        except (ValueError, PermissionError) as exc: QMessageBox.warning(self,"تعذر التحديث",str(exc)); return
        QMessageBox.information(self,"تم التحديث","تم تغيير كلمة المرور.")

    def toggle_user_active(self):
        if not self.require_admin(): return
        user_id = self.selected_user()
        if user_id is None: QMessageBox.information(self,"اختيار حساب","اختار حسابًا من الجدول أولًا."); return
        with get_connection() as con: row = con.execute("SELECT username,role,active FROM users WHERE id=?",(user_id,)).fetchone()
        if not row: return
        if row[1] == "admin": QMessageBox.warning(self,"غير مسموح","لا يمكن إيقاف حساب المدير من هذه الشاشة."); return
        new_status = 0 if row[2] else 1
        try:
            with get_connection() as con: set_user_active(con, actor=self.current_user, target_user_id=user_id, active=bool(new_status))
        except (ValueError, PermissionError) as exc: QMessageBox.warning(self,"تعذر التحديث",str(exc)); return
        self.load_users(); QMessageBox.information(self,"تم التحديث","تم إيقاف الحساب." if not new_status else "تم تفعيل الحساب.")

    def create_home_page(self):
        # Scroll area keeps the full dashboard usable on smaller displays.
        page = QWidget()
        outer_layout = QVBoxLayout(page)
        outer_layout.setContentsMargins(0, 0, 0, 0)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        layout.setContentsMargins(20, 18, 20, 20)
        layout.setSpacing(14)
        layout.setAlignment(Qt.AlignmentFlag.AlignTop)

        header = QHBoxLayout()
        title = QLabel("لوحة التحكم الرئيسية")
        title.setObjectName("PageTitle")
        header.addWidget(title)
        header.addStretch()
        refresh_btn = QPushButton("⟳ تحديث البيانات")
        refresh_btn.setObjectName("Primary")
        refresh_btn.clicked.connect(self.refresh_home_dashboard)
        header.addWidget(refresh_btn)
        layout.addLayout(header)

        desc = QLabel("ملخص سريع للمبيعات والمخزون وأحدث الفواتير.")
        desc.setObjectName("Muted")
        layout.addWidget(desc)

        self.dashboard_period = QLabel("جاري تحميل بيانات اليوم التجاري...")
        self.dashboard_period.setObjectName("PeriodBanner")
        layout.addWidget(self.dashboard_period)

        cards_layout = QGridLayout()
        cards_layout.setSpacing(12)
        self.dashboard_sales = QLabel("—")
        self.dashboard_invoice_count = QLabel("—")
        self.dashboard_returns = QLabel("—")
        self.dashboard_net_sales = QLabel("—")

        cards = [
            ("إجمالي المبيعات", self.dashboard_sales, T.PRIMARY_STRONG),
            ("عدد الفواتير", self.dashboard_invoice_count, T.INFO),
            ("إجمالي المرتجعات", self.dashboard_returns, T.WARNING),
            ("صافي المبيعات بعد المرتجعات", self.dashboard_net_sales, T.SUCCESS),
        ]
        for index, (caption, value_label, accent) in enumerate(cards):
            card = QFrame()
            card.setObjectName("Card")
            card.setMinimumHeight(122)
            card_layout = QVBoxLayout(card)
            card_layout.setContentsMargins(16, 12, 16, 12)
            caption_label = QLabel(caption)
            caption_label.setObjectName("Muted")
            # Align only the text inside each card; do not change the card or
            # dashboard layout direction, which can mirror/reorder the whole UI.
            caption_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            value_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            card_layout.setAlignment(caption_label, Qt.AlignmentFlag.AlignRight)
            card_layout.setAlignment(value_label, Qt.AlignmentFlag.AlignRight)
            caption_label.setStyleSheet("qproperty-alignment: AlignRight;")
            value_label.setStyleSheet(
                f"font-size:21px;font-weight:bold;color:{accent};"
            )
            value_label.setWordWrap(True)
            card_layout.addWidget(caption_label)
            card_layout.addWidget(value_label)
            card_layout.addStretch()
            cards_layout.addWidget(card, 0, index)
        layout.addLayout(cards_layout)

        actions_title = QLabel("الوصول السريع")
        actions_title.setObjectName("SectionTitle")
        layout.addWidget(actions_title)
        actions = QGridLayout()
        actions.setSpacing(10)
        quick_actions = [
            ("فتح الكاشير", self.show_cashier),
            ("المنتجات والمخزون", self.show_products),
            ("الفواتير", self.show_invoices),
            ("التقارير", self.show_reports),
        ]
        for index, (caption, callback) in enumerate(quick_actions):
            button = QPushButton(caption)
            button.setObjectName("Primary")
            button.setMinimumHeight(42)
            button.clicked.connect(callback)
            actions.addWidget(button, 0, index)
        layout.addLayout(actions)

        stock_title = QLabel("تنبيهات المخزون — المنتجات عند حد التنبيه أو أقل")
        stock_title.setObjectName("SectionTitle")
        layout.addWidget(stock_title)
        self.dashboard_stock_table = QTableWidget(0, 3)
        self.dashboard_stock_table.setHorizontalHeaderLabels(
            ["كود المنتج", "اسم المنتج", "الكمية الحالية"]
        )
        self.dashboard_stock_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.dashboard_stock_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.dashboard_stock_table.setAlternatingRowColors(True)
        self.dashboard_stock_table.verticalHeader().setVisible(False)
        self.dashboard_stock_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.dashboard_stock_table.setMinimumHeight(150)
        self.dashboard_stock_table.setMaximumHeight(230)
        layout.addWidget(self.dashboard_stock_table)

        recent_title = QLabel("أحدث الفواتير")
        recent_title.setObjectName("SectionTitle")
        layout.addWidget(recent_title)
        self.dashboard_recent_table = QTableWidget(0, 4)
        self.dashboard_recent_table.setHorizontalHeaderLabels(
            ["رقم الفاتورة", "التاريخ والوقت", "الإجمالي", "طريقة الدفع"]
        )
        self.dashboard_recent_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.dashboard_recent_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.dashboard_recent_table.setAlternatingRowColors(True)
        self.dashboard_recent_table.verticalHeader().setVisible(False)
        self.dashboard_recent_table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch
        )
        self.dashboard_recent_table.setMinimumHeight(180)
        self.dashboard_recent_table.setMaximumHeight(280)
        layout.addWidget(self.dashboard_recent_table)
        layout.addStretch()

        scroll.setWidget(content)
        outer_layout.addWidget(scroll)
        return page

    def create_products_page(self):
        page = QWidget(); layout = QVBoxLayout(page); layout.setSpacing(12)
        header = QHBoxLayout(); title = QLabel("المنتجات والمخزون"); title.setObjectName("PageTitle"); header.addWidget(title); header.addStretch()
        for label, callback in [("＋ إضافة منتج", self.open_add_product_dialog), ("＋ إضافة تصنيف", self.open_add_category_dialog), ("⚙ إدارة التصنيفات", self.manage_categories_dialog), ("✎ تعديل المنتج المحدد", self.open_edit_product_dialog), ("🗑 حذف المنتج المحدد", self.delete_selected_product), ("تحديث البيانات", self.load_products)]:
            b = QPushButton(label); b.setObjectName("Primary"); b.clicked.connect(callback); header.addWidget(b)
        layout.addLayout(header)
        desc = QLabel("تابع كميات المخزون بسرعة، وابحث بالاسم أو الكود أو الباركود، ثم صفِّ حسب التصنيف وحالة المخزون."); desc.setObjectName("Muted"); layout.addWidget(desc)

        stats = QHBoxLayout(); stats.setSpacing(10)
        def make_stock_stat(title_text):
            frame = QFrame(); frame.setObjectName("Card"); box = QVBoxLayout(frame); box.setContentsMargins(14, 10, 14, 10)
            title_label = QLabel(title_text); title_label.setObjectName("Muted")
            value_label = QLabel("0"); value_label.setStyleSheet(T.qss("font-size:23px;font-weight:bold;color:@text@;"))
            box.addWidget(title_label); box.addWidget(value_label); stats.addWidget(frame, 1)
            return value_label
        self.products_total_stat = make_stock_stat("إجمالي المنتجات")
        self.products_low_stat = make_stock_stat("تحتاج إعادة طلب")
        self.products_out_stat = make_stock_stat("نفد المخزون")
        layout.addLayout(stats)

        filters = QHBoxLayout(); filters.setSpacing(8)
        self.search_input = QLineEdit(); self.search_input.setPlaceholderText("ابحث بالاسم أو الكود أو الباركود أو المقاس أو اللون..."); self.search_input.setClearButtonEnabled(True); self.search_input.setMinimumHeight(40); self.search_input.textChanged.connect(self.filter_products); filters.addWidget(self.search_input, 3)
        self.category_filter = QComboBox(); self.category_filter.setMinimumHeight(40); self.category_filter.addItem("كل التصنيفات"); self.category_filter.currentTextChanged.connect(self.filter_products); filters.addWidget(self.category_filter, 1)
        self.stock_filter = QComboBox(); self.stock_filter.setMinimumHeight(40); self.stock_filter.addItems(["كل حالات المخزون", "متوفر", "مخزون منخفض", "نفد المخزون"]); self.stock_filter.currentTextChanged.connect(self.filter_products); filters.addWidget(self.stock_filter, 1)
        clear_filters_btn = QPushButton("مسح الفلاتر")
        clear_filters_btn.clicked.connect(self.clear_product_filters)
        filters.addWidget(clear_filters_btn)
        layout.addLayout(filters)

        self.products_table = QTableWidget(0, 12)
        self.products_table.setHorizontalHeaderLabels(["رقم داخلي", "كود المنتج", "الباركود", "اسم المنتج", "المقاس", "اللون", "التصنيف", "سعر الشراء", "سعر البيع", "الكمية الحالية", "حد إعادة الطلب", "حالة المخزون"])
        self.products_table.setColumnHidden(0, True)
        self.products_table.cellDoubleClicked.connect(lambda *_: self.open_edit_product_dialog())
        self.products_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.products_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.products_table.setAlternatingRowColors(True)
        self.products_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.products_table.verticalHeader().setVisible(False)
        layout.addWidget(self.products_table, 1)
        self.products_status = QLabel(""); self.products_status.setObjectName("Muted"); layout.addWidget(self.products_status)
        return page

    def product_form(self, existing=None, parent=None, purchase_mode=False):
        dialog = QDialog(parent or self); dialog.setWindowTitle("تعديل بيانات المنتج" if existing else "إضافة منتج جديد"); dialog.setMinimumWidth(460); dialog.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        layout = QVBoxLayout(dialog); form = QFormLayout()
        code = QLineEdit(str(existing[0]) if existing else ""); code.setPlaceholderText("مثال: P002")
        barcode = QLineEdit(str(existing[1] or "") if existing else ""); barcode.setPlaceholderText("اختياري — رقم الباركود")
        name = QLineEdit(str(existing[2]) if existing else ""); name.setPlaceholderText("اسم المنتج")
        size = QLineEdit(str(existing[3] or "") if existing else ""); size.setPlaceholderText("اختياري مثل M أو 42")
        color = QLineEdit(str(existing[4] or "") if existing else ""); color.setPlaceholderText("اختياري مثل أسود")
        category = QComboBox(); category.setMinimumHeight(38); category.setEditable(True); category.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        try:
            with get_connection() as con:
                self.ensure_categories_table(con)
                category_names = [str(row[0]) for row in con.execute("SELECT name FROM product_categories ORDER BY name COLLATE NOCASE").fetchall()]
        except Exception:
            category_names = []
        if not category_names:
            category_names = ["عام"]
        category.addItems(category_names)
        category.setCompleter(category.completer())
        category.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        category.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        desired_category = str(existing[5] or "عام") if existing else ("عام" if "عام" in category_names else category_names[0])
        if desired_category not in category_names:
            category.addItem(desired_category)
        category.setCurrentText(desired_category)
        category.setToolTip("اختر تصنيفًا مسجلًا من إدارة التصنيفات.")
        purchase = FocusSelectDoubleSpinBox(); purchase.setRange(0,10000000); purchase.setDecimals(2); purchase.setKeyboardTracking(True); purchase.setSuffix(" جنيه"); purchase.setValue((existing[6] / 100) if existing else 0)
        if existing:
            purchase.setEnabled(False)
            purchase.setToolTip("تكلفة المخزون متوسط مرجح وتُحدّث تلقائيًا مع فواتير الشراء")
        selling = FocusSelectDoubleSpinBox(); selling.setRange(0,10000000); selling.setDecimals(2); selling.setKeyboardTracking(True); selling.setSuffix(" جنيه"); selling.setValue((existing[7] / 100) if existing else 0)
        quantity = QSpinBox(); quantity.setRange(0,10000000); quantity.setValue(existing[8] if existing else 0)
        minimum = QSpinBox(); minimum.setRange(0,10000000); minimum.setValue(existing[9] if existing else 0)
        product_fields = [("كود المنتج:",code),("الباركود:",barcode),("اسم المنتج:",name),("المقاس (اختياري):",size),("اللون (اختياري):",color),("التصنيف:",category),("سعر الشراء:",purchase),("سعر البيع:",selling)]
        if not purchase_mode:
            product_fields.append(("الكمية الحالية:",quantity))
        product_fields.append(("حد التنبيه للمخزون:",minimum))
        for label, widget in product_fields: form.addRow(label,widget)
        if purchase_mode:
            purchase_note = QLabel("كمية المخزون ستُضاف من الكمية التي تدخلها في فاتورة الشراء بعد الضغط على حفظ.")
            purchase_note.setWordWrap(True); purchase_note.setObjectName("Muted"); layout.addWidget(purchase_note)
        layout.addLayout(form)
        category_note = QLabel("التصنيف مرتبط بقائمة التصنيفات المسجلة. لإضافة أو حذف تصنيف استخدم إدارة التصنيفات في صفحة المنتجات.")
        category_note.setObjectName("Muted"); category_note.setWordWrap(True); layout.addWidget(category_note)
        buttons=QHBoxLayout(); save=QPushButton("حفظ المنتج" if not existing else "حفظ التعديلات"); save.setObjectName("Primary"); cancel=QPushButton("إلغاء"); buttons.addWidget(save); buttons.addWidget(cancel); layout.addLayout(buttons); cancel.clicked.connect(dialog.reject)
        result = {}
        def save_form():
            code_value=code.text().strip(); name_value=name.text().strip(); barcode_value=barcode.text().strip()
            if not code_value or not name_value:
                QMessageBox.warning(dialog,"بيانات ناقصة","من فضلك أدخل كود المنتج واسمه."); return
            category_value = category.currentText().strip()
            if not category_value:
                QMessageBox.warning(dialog,"تصنيف مطلوب","أضف تصنيفًا من صفحة المنتجات أولًا."); return
            if category_value.casefold() not in {x.casefold() for x in category_names}:
                QMessageBox.warning(dialog,"تصنيف غير مسجل","اختار تصنيفًا من القائمة، أو أضف التصنيف من إدارة التصنيفات أولًا."); return
            category_value = next(x for x in category_names if x.casefold() == category_value.casefold())
            result.update(code=code_value, barcode=barcode_value or None, name=name_value, size=size.text().strip(), color=color.text().strip(), category=category_value, purchase=to_piasters(purchase.value()), selling=to_piasters(selling.value()), quantity=quantity.value(), minimum=minimum.value())
            dialog.accept()
        save.clicked.connect(save_form); dialog.exec(); return result if result else None

    def ensure_categories_table(self, con):
        # Separate category registry allows adding categories before the first product is assigned.
        con.execute("CREATE TABLE IF NOT EXISTS product_categories (name TEXT PRIMARY KEY COLLATE NOCASE)")
        con.execute("INSERT OR IGNORE INTO product_categories(name) SELECT DISTINCT COALESCE(NULLIF(trim(category),''),'عام') FROM products")

    def open_add_category_dialog(self):
        if not self.require_admin(): return
        category, ok = QInputDialog.getText(self, "إضافة تصنيف جديد", "اسم التصنيف:")
        if not ok:
            return
        category = category.strip()
        if not category:
            QMessageBox.warning(self, "اسم التصنيف مطلوب", "اكتب اسم التصنيف أولًا.")
            return
        try:
            with get_connection() as con:
                catalog_service.add_category(con, actor=self.current_user, name=category)
            self.load_products()
            if hasattr(self, "category_filter"):
                self.category_filter.setCurrentText(category)
            QMessageBox.information(self, "تمت الإضافة", f"تم إضافة التصنيف «{category}» بنجاح. تقدر تختاره عند إضافة أو تعديل منتج.")
        except (ValueError, PermissionError) as e:
            QMessageBox.warning(self, "تعذر إضافة التصنيف", str(e))
        except Exception as e:
            QMessageBox.critical(self, "خطأ", f"تعذر إضافة التصنيف:\n{e}")

    def manage_categories_dialog(self):
        if not self.require_admin(): return
        dialog = QDialog(self); dialog.setWindowTitle("إدارة التصنيفات"); dialog.setMinimumSize(520, 420); dialog.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        layout = QVBoxLayout(dialog)
        title = QLabel("إدارة تصنيفات المنتجات"); title.setObjectName("PageTitle"); layout.addWidget(title)
        info = QLabel("أضف التصنيفات التي تحتاجها أو احذف التصنيف غير المستخدم. لا يمكن حذف تصنيف مرتبط بمنتجات؛ غيّر تصنيف المنتجات أولًا حتى لا تضيع بياناتها.")
        info.setObjectName("Muted"); info.setWordWrap(True); layout.addWidget(info)
        table = QTableWidget(0, 2); table.setHorizontalHeaderLabels(["اسم التصنيف", "عدد المنتجات المرتبطة"])
        table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows); table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection); table.setStyleSheet(T.qss("QTableWidget::item:selected { background:@primary@; color:@text_on_primary@; }")); table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers); table.verticalHeader().setVisible(False)
        layout.addWidget(table, 1)
        actions = QHBoxLayout(); add_btn = QPushButton("＋ إضافة تصنيف"); add_btn.setObjectName("Primary"); rename_btn = QPushButton("✎ تعديل الاسم"); rename_btn.setObjectName("Primary"); delete_btn = QPushButton("حذف التصنيف المحدد"); delete_btn.setObjectName("Danger"); refresh_btn = QPushButton("تحديث"); close_btn = QPushButton("إغلاق")
        actions.addWidget(add_btn); actions.addWidget(rename_btn); actions.addWidget(delete_btn); actions.addWidget(refresh_btn); actions.addWidget(close_btn); layout.addLayout(actions)
        def refresh_categories():
            try:
                with get_connection() as con:
                    self.ensure_categories_table(con)
                    rows = con.execute("SELECT c.name, (SELECT COUNT(*) FROM products p WHERE COALESCE(NULLIF(trim(p.category),''),'عام')=c.name COLLATE NOCASE) FROM product_categories c ORDER BY c.name COLLATE NOCASE").fetchall()
                table.setRowCount(len(rows))
                for i, (name, count) in enumerate(rows):
                    table.setItem(i, 0, QTableWidgetItem(str(name)))
                    table.setItem(i, 1, QTableWidgetItem(str(count)))
                    table.item(i, 0).setTextAlignment(Qt.AlignmentFlag.AlignCenter); table.item(i, 1).setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            except Exception as e:
                QMessageBox.critical(dialog, "خطأ", f"تعذر تحميل التصنيفات:\n{e}")
        def add_from_manager():
            category, ok = QInputDialog.getText(dialog, "إضافة تصنيف جديد", "اسم التصنيف:")
            if not ok: return
            category = category.strip()
            if not category:
                QMessageBox.warning(dialog, "اسم التصنيف مطلوب", "اكتب اسم التصنيف أولًا."); return
            try:
                with get_connection() as con:
                    catalog_service.add_category(con, actor=self.current_user, name=category)
                refresh_categories(); self.load_products()
            except (ValueError, PermissionError) as e:
                QMessageBox.warning(dialog, "تعذر إضافة التصنيف", str(e))
            except Exception as e:
                QMessageBox.critical(dialog, "خطأ", f"تعذر إضافة التصنيف:\n{e}")
        def rename_selected_category():
            row = table.currentRow()
            if row < 0:
                QMessageBox.information(dialog, "اختيار تصنيف", "اختار التصنيف اللي عايز تعدل اسمه أولًا."); return
            old_name = table.item(row, 0).text()
            if old_name.casefold() == "عام":
                QMessageBox.warning(dialog, "تصنيف افتراضي", "لا يمكن تغيير اسم التصنيف الافتراضي «عام» حفاظًا على المنتجات التي لا تملك تصنيفًا محددًا."); return
            new_name, ok = QInputDialog.getText(dialog, "تعديل اسم التصنيف", "الاسم الجديد للتصنيف:", text=old_name)
            if not ok: return
            new_name = new_name.strip()
            if not new_name:
                QMessageBox.warning(dialog, "اسم التصنيف مطلوب", "اكتب اسمًا للتصنيف."); return
            if new_name.casefold() == old_name.casefold(): return
            try:
                with get_connection() as con:
                    # Admin check + rename of the category and its products in one audited transaction.
                    catalog_service.rename_category(con, actor=self.current_user, old_name=old_name, new_name=new_name)
                refresh_categories(); self.load_products()
                if hasattr(self, "cashier_search"):
                    self.refresh_cashier_products()
                QMessageBox.information(dialog, "تم التعديل", f"تم تغيير اسم التصنيف من «{old_name}» إلى «{new_name}» وتحديث المنتجات المرتبطة به.")
            except (ValueError, PermissionError) as e:
                QMessageBox.warning(dialog, "تعذر تعديل التصنيف", str(e))
            except Exception as e:
                QMessageBox.critical(dialog, "خطأ", f"تعذر تعديل اسم التصنيف:\n{e}")
        def delete_selected_category():
            row = table.currentRow()
            if row < 0:
                QMessageBox.information(dialog, "اختيار تصنيف", "اختار التصنيف اللي عايز تحذفه أولًا."); return
            category = table.item(row, 0).text()
            try:
                with get_connection() as con:
                    self.ensure_categories_table(con)
                    count = con.execute("SELECT COUNT(*) FROM products WHERE COALESCE(NULLIF(trim(category),''),'عام')=? COLLATE NOCASE", (category,)).fetchone()[0]
                    if count:
                        QMessageBox.warning(dialog, "التصنيف مستخدم", f"لا يمكن حذف «{category}» لأنه مرتبط بـ {count} منتج. غيّر تصنيف هذه المنتجات أولًا ثم احذف التصنيف."); return
                    if category.casefold() == "عام":
                        QMessageBox.warning(dialog, "تصنيف افتراضي", "لا يمكن حذف تصنيف «عام» الافتراضي."); return
                    answer = QMessageBox.question(dialog, "تأكيد الحذف", f"هل تريد حذف التصنيف «{category}»؟", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
                    if answer != QMessageBox.StandardButton.Yes: return
                    catalog_service.delete_category(con, actor=self.current_user, name=category)
                refresh_categories(); self.load_products()
            except (ValueError, PermissionError) as e:
                QMessageBox.warning(dialog, "تعذر حذف التصنيف", str(e))
            except Exception as e:
                QMessageBox.critical(dialog, "خطأ", f"تعذر حذف التصنيف:\n{e}")
        add_btn.clicked.connect(add_from_manager); rename_btn.clicked.connect(rename_selected_category); delete_btn.clicked.connect(delete_selected_category); refresh_btn.clicked.connect(refresh_categories); close_btn.clicked.connect(dialog.accept)
        refresh_categories(); dialog.exec()

    def open_add_product_dialog(self):
        if not self.require_admin(): return
        data=self.product_form()
        if not data: return
        try:
            with get_connection() as con:
                catalog_service.create_product(con, actor=self.current_user, code=data['code'], barcode=data['barcode'], name=data['name'],
                    size=data['size'], color=data['color'], category=data['category'], purchase_price=data['purchase'],
                    selling_price=data['selling'], quantity=data['quantity'], min_stock=data['minimum'])
            QMessageBox.information(self,"تم الحفظ","تمت إضافة المنتج بنجاح."); self.load_products()
        except sqlite3.IntegrityError as e:
            message = "كود المنتج مستخدم بالفعل." if "code" in str(e).lower() else "الباركود مستخدم بالفعل لمنتج آخر."
            QMessageBox.warning(self,"كود أو باركود مكرر",message)
        except PermissionError as e: QMessageBox.warning(self,"غير مسموح",str(e))
        except ValueError as e: QMessageBox.warning(self,"بيانات غير صالحة",str(e))
        except Exception as e: QMessageBox.critical(self,"خطأ",f"تعذر حفظ المنتج:\n{e}")

    def open_edit_product_dialog(self):
        if not self.require_admin(): return
        row=self.products_table.currentRow()
        if row<0: QMessageBox.information(self,"اختيار منتج","اختار المنتج اللي عايز تعدله أولًا."); return
        product_id=int(self.products_table.item(row,0).text())
        try:
            with get_connection() as con:
                p=con.execute("SELECT code,barcode,name,size,color,COALESCE(category,'عام'),purchase_price,selling_price,stock_quantity,min_stock FROM products WHERE id=?",(product_id,)).fetchone()
        except Exception as e: QMessageBox.critical(self,"خطأ",str(e)); return
        if not p: self.load_products(); return
        data=self.product_form(p)
        if not data: return
        stock_reason=""
        if data['quantity'] != p[8]:
            # Ask BEFORE any database transaction starts: a modal dialog must never hold the SQLite write lock.
            stock_reason,ok=QInputDialog.getText(self,"سبب تعديل المخزون","اكتب سبب تعديل كمية المنتج:")
            if not ok or not stock_reason.strip():
                QMessageBox.warning(self,"تم الإلغاء","تم إلغاء الحفظ لعدم إدخال سبب تعديل المخزون."); return
        try:
            with get_connection() as con:
                catalog_service.update_product(con, actor=self.current_user, product_id=product_id, code=data['code'], barcode=data['barcode'],
                    name=data['name'], size=data['size'], color=data['color'], category=data['category'], selling_price=data['selling'],
                    min_stock=data['minimum'], new_quantity=data['quantity'], stock_reason=stock_reason.strip())
            QMessageBox.information(self,"تم الحفظ","تم تعديل بيانات المنتج بنجاح."); self.load_products(); self.refresh_cashier_products()
        except sqlite3.IntegrityError:
            QMessageBox.warning(self,"كود أو باركود مكرر","الكود أو الباركود مستخدم بالفعل لمنتج آخر.")
        except PermissionError as e: QMessageBox.warning(self,"غير مسموح",str(e))
        except ValueError as e: QMessageBox.warning(self,"بيانات غير صالحة",str(e))
        except Exception as e: QMessageBox.critical(self,"خطأ",f"تعذر حفظ التعديلات:\n{e}")

    def delete_selected_product(self):
        if not self.require_admin(): return
        row = self.products_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "اختيار منتج", "اختار المنتج الذي تريد حذفه أولًا.")
            return
        product_id = int(self.products_table.item(row, 0).text())
        product_name = self.products_table.item(row, 3).text() if self.products_table.item(row, 3) else "المنتج المحدد"
        answer = QMessageBox.question(self, "تأكيد حذف المنتج", f"هل تريد حذف «{product_name}»؟\n\nلن يتم حذف أي منتج مرتبط بفواتير أو مشتريات أو مرتجعات قديمة.", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            with get_connection() as con:
                catalog_service.delete_product(con, actor=self.current_user, product_id=product_id)
            self.load_products()
            self.refresh_cashier_products()
            QMessageBox.information(self, "تم الحذف", "تم حذف المنتج.")
        except (ValueError, PermissionError) as e:
            QMessageBox.warning(self, "الحذف غير مسموح", str(e))
        except Exception as e:
            QMessageBox.critical(self, "تعذر الحذف", f"لم يتم حذف المنتج:\n{e}")

    def load_products(self):
        if not self.require_admin(): return
        try:
            with get_connection() as con:
                self.ensure_categories_table(con)
                rows = con.execute("SELECT id,code,COALESCE(barcode,''),name,COALESCE(size,''),COALESCE(color,''),COALESCE(NULLIF(trim(category),''),'عام'),purchase_price,selling_price,stock_quantity,COALESCE(min_stock,0) FROM products ORDER BY id DESC").fetchall()
                categories = [r[0] for r in con.execute("SELECT name FROM product_categories UNION SELECT DISTINCT COALESCE(NULLIF(trim(category),''),'عام') FROM products ORDER BY 1 COLLATE NOCASE").fetchall()]
            self.products_table.setRowCount(len(rows))
            categories = sorted({str(c or 'عام') for c in categories}, key=str.casefold)
            current_category = self.category_filter.currentText() if hasattr(self, 'category_filter') else "كل التصنيفات"
            self.category_filter.blockSignals(True)
            self.category_filter.clear(); self.category_filter.addItem("كل التصنيفات"); self.category_filter.addItems(categories)
            self.category_filter.setCurrentText(current_category if current_category == "كل التصنيفات" or current_category in categories else "كل التصنيفات")
            self.category_filter.blockSignals(False)
            low_count = sum(1 for p in rows if int(p[9] or 0) > 0 and int(p[9] or 0) <= int(p[10] or 0))
            out_count = sum(1 for p in rows if int(p[9] or 0) <= 0)
            self.products_total_stat.setText(str(len(rows)))
            self.products_low_stat.setText(str(low_count))
            self.products_out_stat.setText(str(out_count))
            for r, p in enumerate(rows):
                qty, minimum = int(p[9] or 0), int(p[10] or 0)
                status = "نفد المخزون" if qty <= 0 else ("مخزون منخفض" if qty <= minimum else "متوفر")
                cost_unknown = qty > 0 and int(p[7] or 0) <= 0     # stock on hand but no trustworthy cost
                values = [p[0], p[1], p[2], p[3], p[4], p[5], p[6], "⚠ تكلفة غير معروفة" if cost_unknown else money(p[7]), money(p[8]), qty, minimum, status]
                for c, v in enumerate(values):
                    item = QTableWidgetItem(str(v)); item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    if c == 7 and cost_unknown:
                        item.setForeground(Qt.GlobalColor.red); item.setToolTip("المخزون الحالي بلا تكلفة معروفة: لا تُحسب أرباحه ولن يُستلم عليه شراء قبل تحديد التكلفة.")
                    if c == 11:
                        if status == "نفد المخزون": item.setForeground(Qt.GlobalColor.red)
                        elif status == "مخزون منخفض": item.setForeground(Qt.GlobalColor.darkYellow)
                    self.products_table.setItem(r, c, item)
            self.filter_products(self.search_input.text())
        except Exception as e:
            QMessageBox.critical(self, "خطأ في قاعدة البيانات", f"تعذر تحميل المنتجات:\n{e}")

    def clear_product_filters(self):
        if hasattr(self, "search_input"):
            self.search_input.clear()
        if hasattr(self, "category_filter"):
            self.category_filter.setCurrentIndex(0)
        if hasattr(self, "stock_filter"):
            self.stock_filter.setCurrentIndex(0)
        self.filter_products("")

    def filter_products(self, text=""):
        search = str(text).strip().casefold()
        if not hasattr(self, "products_table"):
            return
        category = self.category_filter.currentText() if hasattr(self, "category_filter") else "كل التصنيفات"
        stock_status = self.stock_filter.currentText() if hasattr(self, "stock_filter") else "كل حالات المخزون"
        visible = 0
        for row in range(self.products_table.rowCount()):
            values = [self.products_table.item(row, c).text().casefold() if self.products_table.item(row, c) else "" for c in range(1, self.products_table.columnCount())]
            row_category = self.products_table.item(row, 6).text() if self.products_table.item(row, 6) else ""
            row_status = self.products_table.item(row, 11).text() if self.products_table.item(row, 11) else ""
            matches_search = not search or any(search in value for value in values)
            matches_category = category == "كل التصنيفات" or row_category == category
            matches_status = stock_status == "كل حالات المخزون" or row_status == stock_status
            hidden = not (matches_search and matches_category and matches_status)
            self.products_table.setRowHidden(row, hidden)
            if not hidden: visible += 1
        if hasattr(self, "products_status"):
            self.products_status.setText(f"المعروض: {visible} من {self.products_table.rowCount()} منتج — يمكنك الضغط مرتين على المنتج لتعديله.")

    def create_cashier_page(self):
        page = QWidget(); root = QVBoxLayout(page); root.setContentsMargins(0, 0, 0, 0); root.setSpacing(10)
        cashier_header = QHBoxLayout()
        title = QLabel("الكاشير"); title.setObjectName("PageTitle"); cashier_header.addWidget(title); cashier_header.addStretch()
        self.cashier_shift_button = QPushButton("🕒 فتح وردية / الخزنة"); self.cashier_shift_button.setObjectName("Primary"); self.cashier_shift_button.clicked.connect(self.show_shifts); cashier_header.addWidget(self.cashier_shift_button)
        root.addLayout(cashier_header)
        hint = QLabel("اضغط على أي منتج لإضافته مباشرةً إلى الفاتورة. افتح ورديتك أولًا، ثم استخدم التصنيفات أو البحث السريع."); hint.setObjectName("Muted"); root.addWidget(hint)
        body = QHBoxLayout(); body.setSpacing(14)
        # Invoice/payment panel sits at the right side in the RTL cashier screen.
        #
        # Layout contract (see docs/POS_UI_LAYOUT.md):
        #   invoice_panel (QVBoxLayout)
        #     |- invoice_scroll  -> invoice_body (QVBoxLayout): header, cart table, cart actions,
        #     |                     discount section, customer/payment section.  Scrolls vertically
        #     |                     whenever the window is too short; nothing is ever squeezed
        #     |                     below its minimum size, so nothing can overlap.
        #     `- footer (always visible, never scrolls): final total + checkout button.
        invoice_panel = QFrame(); invoice_panel.setObjectName("InvoicePanel"); invoice_panel.setMinimumWidth(385); invoice_panel.setMaximumWidth(485)
        invoice_panel.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        self.invoice_panel = invoice_panel
        panel_layout = QVBoxLayout(invoice_panel); panel_layout.setContentsMargins(12, 12, 12, 12); panel_layout.setSpacing(8)

        self.invoice_scroll = QScrollArea(); self.invoice_scroll.setObjectName("InvoiceScroll")
        self.invoice_scroll.setWidgetResizable(True); self.invoice_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.invoice_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.invoice_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.invoice_scroll.setMinimumHeight(160)
        self.invoice_scroll.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        invoice_body = QWidget(); invoice_body.setObjectName("InvoiceScrollContent"); self.invoice_body = invoice_body
        invoice_layout = QVBoxLayout(invoice_body); invoice_layout.setContentsMargins(0, 0, 0, 0); invoice_layout.setSpacing(8)
        self.invoice_scroll.setWidget(invoice_body)
        invoice_body.setAutoFillBackground(False); self.invoice_scroll.viewport().setAutoFillBackground(False)
        panel_layout.addWidget(self.invoice_scroll, 1)

        # 1) Current invoice: title + item/unit counters on one row.
        header_row = QHBoxLayout(); header_row.setSpacing(8)
        carttitle = QLabel("الفاتورة الحالية"); carttitle.setObjectName("InvoiceTitle"); self.cart_title_label = carttitle
        self.cart_summary_label = QLabel("الأصناف: 0  |  إجمالي القطع: 0"); self.cart_summary_label.setObjectName("InvoiceChip")
        header_row.addWidget(carttitle); header_row.addStretch(1); header_row.addWidget(self.cart_summary_label)
        invoice_layout.addLayout(header_row)

        # 1b) Cart table: absorbs spare height, but never shrinks below header + two rows.
        self.cart_table = QTableWidget(0, 6)
        self.cart_table.setHorizontalHeaderLabels(["ID", "المنتج", "مقاس/لون", "السعر", "الكمية", "الإجمالي"])
        for column, tip in ((2, "المقاس / اللون"), (3, "سعر الوحدة")):
            self.cart_table.horizontalHeaderItem(column).setToolTip(tip)
        self.cart_table.setColumnHidden(0, True); self.cart_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows); self.cart_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        # Column sizing contract lives in CartColumnFitter (content-sized money columns, stretch
        # product name with a floor, horizontal scrolling as the last resort).
        self.cart_column_fitter = CartColumnFitter(self.cart_table)
        self.cart_table.verticalHeader().setVisible(False); self.cart_table.setAlternatingRowColors(True); self.cart_table.doubleClicked.connect(self.change_cart_quantity)
        self.cart_table.setMinimumHeight(132); self.cart_table.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        invoice_layout.addWidget(self.cart_table, 1)

        # 2) Cart item actions: two explicit rows, equal widths, identical heights.
        actions = QVBoxLayout(); actions.setSpacing(6); actions.setContentsMargins(0, 0, 0, 0)
        action_rows = (
            (("＋ زيادة", self.increase_cart_quantity, "Primary", "زيادة كمية الصنف المحدد بمقدار 1"),
             ("− تقليل", self.decrease_cart_quantity, "Primary", "تقليل كمية الصنف المحدد بمقدار 1"),
             ("تعديل الكمية", self.change_cart_quantity, "Primary", "تعديل كمية الصنف المحدد يدويًا")),
            (("حذف الصنف", self.remove_cart_item, "Danger", "حذف الصنف المحدد من الفاتورة"),
             ("تفريغ الفاتورة", self.clear_cart, "Danger", "إزالة كل الأصناف من الفاتورة الحالية")),
        )
        self.cart_action_buttons = []
        for row_specs in action_rows:
            row = QHBoxLayout(); row.setSpacing(6); row.setContentsMargins(0, 0, 0, 0)
            for label, fn, obj, tip in row_specs:
                b = QPushButton(label); b.setObjectName(obj); b.setToolTip(tip)
                b.setStyleSheet("padding: 8px 6px;")            # keeps the theme colours, leaves room for the text
                b.setMinimumHeight(40); b.setMaximumHeight(46)
                b.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
                b.clicked.connect(fn)
                row.addWidget(b, 1); self.cart_action_buttons.append(b)
            actions.addLayout(row)
        invoice_layout.addLayout(actions)

        # 3) Subtotal + discount (grouped, so the cashier reads them as one block).
        discount_box = QFrame(); discount_box.setObjectName("InvoiceSection"); self.discount_box = discount_box
        discount_layout = QVBoxLayout(discount_box); discount_layout.setContentsMargins(10, 8, 10, 8); discount_layout.setSpacing(6)
        self.subtotal_label = QLabel("الإجمالي قبل الخصم: 0.00 جنيه"); self.subtotal_label.setObjectName("InvoiceSubtotal")
        self.subtotal_label.setWordWrap(True); discount_layout.addWidget(self.subtotal_label)
        self.discount_input = FocusSelectDoubleSpinBox(); self.discount_input.setRange(0,10000000); self.discount_input.setDecimals(2); self.discount_input.setKeyboardTracking(True); self.discount_input.setPrefix("الخصم: "); self.discount_input.setSuffix(" جنيه"); self.discount_input.setMinimumHeight(42); self.discount_input.setToolTip("اضغط داخل الحقل واكتب قيمة الخصم مباشرةً"); self.discount_input.valueChanged.connect(self.update_cart_totals)
        self.discount_input.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum); discount_layout.addWidget(self.discount_input)
        self.discount_summary_label = QLabel("قيمة الخصم: 0.00 جنيه"); self.discount_summary_label.setObjectName("InvoiceDiscountValue")
        self.discount_summary_label.setWordWrap(True); discount_layout.addWidget(self.discount_summary_label)
        invoice_layout.addWidget(discount_box)

        # 4) Customer, payment method, paid amount and remaining/change.
        payment_box = QFrame(); payment_box.setObjectName("InvoiceSection"); self.payment_box = payment_box
        payment_layout = QVBoxLayout(payment_box); payment_layout.setContentsMargins(10, 8, 10, 8); payment_layout.setSpacing(6)
        self.customer_label = QLabel("العميل (مطلوب للآجل):"); self.customer_label.setObjectName("InvoiceFieldLabel"); payment_layout.addWidget(self.customer_label)
        self.customer_combo = QComboBox(); self.customer_combo.setEditable(True); self.customer_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert); self.customer_combo.setMinimumHeight(38); self.customer_combo.setToolTip("اختر عميلًا أو اكتب اسمه. الاسم مطلوب عند البيع الآجل.")
        self.customer_combo.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum); payment_layout.addWidget(self.customer_combo)
        self.payment_label = QLabel("طريقة الدفع:"); self.payment_label.setObjectName("InvoiceFieldLabel"); payment_layout.addWidget(self.payment_label)
        self.payment_method = QComboBox(); self.payment_method.addItems([PAYMENT_CASH, PAYMENT_CREDIT, PAYMENT_SPLIT]); self.payment_method.setMinimumHeight(38); self.payment_method.currentTextChanged.connect(self.on_payment_method_changed)
        self.payment_method.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum); payment_layout.addWidget(self.payment_method)
        self.paid_input = FocusSelectDoubleSpinBox(); self.paid_input.setRange(0,10000000); self.paid_input.setDecimals(2); self.paid_input.setKeyboardTracking(True); self.paid_input.setPrefix("المدفوع: "); self.paid_input.setSuffix(" جنيه"); self.paid_input.setMinimumHeight(42); self.paid_input.setToolTip("اضغط داخل الحقل واكتب المبلغ مباشرةً"); self.paid_input.valueChanged.connect(self.update_cart_totals)
        self.paid_input.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum); payment_layout.addWidget(self.paid_input); self.on_payment_method_changed(self.payment_method.currentText())
        self.change_label = QLabel("الباقي: 0.00 جنيه"); self.change_label.setObjectName("InvoiceChange"); self.change_label.setWordWrap(True); payment_layout.addWidget(self.change_label)
        invoice_layout.addWidget(payment_box)

        # 5) Footer, outside the scroll area: final total and checkout are always on screen.
        footer = QVBoxLayout(); footer.setSpacing(8); footer.setContentsMargins(0, 0, 0, 0)
        self.total_label = QLabel("الإجمالي المطلوب: 0.00 جنيه"); self.total_label.setObjectName("InvoiceTotal")
        self.total_label.setAlignment(Qt.AlignmentFlag.AlignCenter); self.total_label.setWordWrap(True)
        self.total_label.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        footer.addWidget(self.total_label)
        checkout = QPushButton("حفظ الفاتورة وإنهاء البيع"); checkout.setObjectName("Primary"); checkout.setMinimumHeight(48); checkout.clicked.connect(self.checkout); self.checkout_button = checkout
        checkout.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        footer.addWidget(checkout)
        panel_layout.addLayout(footer)
        body.addWidget(invoice_panel, 2)
        # Product browser: search, category chips, and clickable product tiles.
        product_area = QWidget(); product_layout = QVBoxLayout(product_area); product_layout.setContentsMargins(0,0,0,0); product_layout.setSpacing(10)
        searchrow = QHBoxLayout(); self.cashier_search = QLineEdit(); self.cashier_search.setPlaceholderText("بحث بالاسم أو الكود أو الباركود..."); self.cashier_search.setMinimumHeight(42); self.cashier_search.setClearButtonEnabled(True); self.cashier_search.textChanged.connect(self.on_cashier_search_changed); self.cashier_search.returnPressed.connect(self.add_search_result); searchrow.addWidget(self.cashier_search, 1)
        scan_btn = QPushButton("إضافة بالكود"); scan_btn.setObjectName("Primary"); scan_btn.clicked.connect(self.add_search_result); searchrow.addWidget(scan_btn); product_layout.addLayout(searchrow)
        category_scroll = QScrollArea(); category_scroll.setWidgetResizable(True); category_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded); category_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff); category_scroll.setFixedHeight(56)
        category_widget = QWidget(); self.category_row = QHBoxLayout(category_widget); self.category_row.setContentsMargins(4,4,4,4); self.category_row.setSpacing(7); category_scroll.setWidget(category_widget); product_layout.addWidget(category_scroll)
        self.products_scroll = QScrollArea(); self.products_scroll.setWidgetResizable(True); self.products_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.products_container = QWidget(); self.products_grid = QGridLayout(self.products_container); self.products_grid.setContentsMargins(4,4,4,4); self.products_grid.setSpacing(12); self.products_scroll.setWidget(self.products_container); product_layout.addWidget(self.products_scroll, 1)
        pager = QHBoxLayout()
        self.cashier_prev_page = QPushButton("السابق"); self.cashier_prev_page.clicked.connect(self.previous_cashier_page); pager.addWidget(self.cashier_prev_page)
        self.cashier_page_label = QLabel("صفحة 1"); self.cashier_page_label.setAlignment(Qt.AlignmentFlag.AlignCenter); pager.addWidget(self.cashier_page_label,1)
        self.cashier_next_page = QPushButton("التالي"); self.cashier_next_page.clicked.connect(self.next_cashier_page); pager.addWidget(self.cashier_next_page)
        product_layout.addLayout(pager)
        self.cashier_status = QLabel(""); self.cashier_status.setObjectName("Muted"); product_layout.addWidget(self.cashier_status)
        product_area.setMinimumWidth(300)
        body.addWidget(product_area, 3); root.addLayout(body, 1)
        self.cart = {}; self.selected_category = "الكل"; self.category_buttons = []; self.cashier_page_size=120; self.cashier_page_index=0; self.update_cart_totals(); self.refresh_customer_combo(); self.refresh_cashier_products(); return page

    def rebuild_categories(self, categories):
        while self.category_row.count():
            item = self.category_row.takeAt(0)
            widget = item.widget()
            if widget: widget.deleteLater()
        self.category_buttons = []
        names = ["الكل"] + [c for c in categories if c and c != "الكل"]
        if self.selected_category not in names: self.selected_category = "الكل"
        for category in names:
            btn = QPushButton(category); btn.setObjectName("CategoryButton"); btn.setCursor(Qt.CursorShape.PointingHandCursor); btn.setCheckable(True); btn.setChecked(category == self.selected_category); btn.clicked.connect(lambda checked=False, c=category: self.select_category(c)); self.category_row.addWidget(btn); self.category_buttons.append(btn)
        self.category_row.addStretch(1)

    def select_category(self, category):
        self.selected_category = category
        self.cashier_page_index = 0
        for btn in self.category_buttons: btn.setChecked(btn.text() == category)
        self.refresh_cashier_products()

    def on_cashier_search_changed(self, _text=""):
        self.cashier_page_index = 0
        self.refresh_cashier_products()

    def previous_cashier_page(self):
        self.cashier_page_index = max(0,self.cashier_page_index-1)
        self.refresh_cashier_products()

    def next_cashier_page(self):
        self.cashier_page_index += 1
        self.refresh_cashier_products()

    def on_payment_method_changed(self, method):
        if not hasattr(self,"paid_input"):
            return
        is_credit = method == PAYMENT_CREDIT
        self.paid_input.setEnabled(not is_credit)
        if is_credit:
            self.paid_input.setValue(0)
        self.update_cart_totals()

    def refresh_cashier_products(self):
        if not hasattr(self, "products_grid"): return
        search = self.cashier_search.text().strip().casefold() if hasattr(self, "cashier_search") else ""
        try:
            with get_connection() as con:
                self.ensure_categories_table(con)
                categories = [r[0] for r in con.execute("SELECT name FROM product_categories UNION SELECT DISTINCT COALESCE(NULLIF(trim(category),''),'عام') FROM products ORDER BY 1 COLLATE NOCASE").fetchall()]
                query = "SELECT id,code,COALESCE(barcode,''),name,COALESCE(size,''),COALESCE(color,''),COALESCE(NULLIF(trim(category),''),'عام'),stock_quantity,selling_price FROM products WHERE stock_quantity > 0"
                params = []
                if self.selected_category != "الكل": query += " AND COALESCE(NULLIF(trim(category),''),'عام')=?"; params.append(self.selected_category)
                if search:
                    pat = f"%{search}%"; query += " AND (lower(code) LIKE ? OR lower(COALESCE(barcode,'')) LIKE ? OR lower(name) LIKE ? OR lower(COALESCE(size,'')) LIKE ? OR lower(COALESCE(color,'')) LIKE ?)"; params.extend([pat,pat,pat,pat,pat])
                total_count = con.execute("SELECT COUNT(*) FROM (" + query + ")", params).fetchone()[0]
                page_count = max(1,(total_count + self.cashier_page_size - 1)//self.cashier_page_size)
                self.cashier_page_index = min(self.cashier_page_index,page_count-1)
                query += " ORDER BY name COLLATE NOCASE LIMIT ? OFFSET ?"
                page_params = params + [self.cashier_page_size,self.cashier_page_index*self.cashier_page_size]
                rows = con.execute(query, page_params).fetchall()
            self.rebuild_categories(categories)
            while self.products_grid.count():
                item = self.products_grid.takeAt(0); widget = item.widget()
                if widget: widget.deleteLater()
            columns = 3 if self.width() >= 1180 else 2
            for index, product in enumerate(rows):
                pid, code, barcode, name, size, color, category, stock, price = product
                variant = " • ".join(v for v in (size, color) if v)
                label = f"{name}\n{variant + chr(10) if variant else ''}{money(price)}\nالمتاح: {stock}"
                tile = QPushButton(label); tile.setObjectName("ProductTile"); tile.setCursor(Qt.CursorShape.PointingHandCursor); tile.setMinimumHeight(126); tile.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed); tile.setToolTip(f"{name} | كود: {code} | باركود: {barcode or '—'} | التصنيف: {category}"); tile.clicked.connect(lambda checked=False, product_id=pid: self.add_product_to_cart(product_id)); self.products_grid.addWidget(tile, index // columns, index % columns)
            self.cashier_page_label.setText(f"صفحة {self.cashier_page_index+1} من {page_count}")
            self.cashier_prev_page.setEnabled(self.cashier_page_index>0)
            self.cashier_next_page.setEnabled(self.cashier_page_index<page_count-1)
            self.cashier_status.setText(f"المنتجات المعروضة: {len(rows)} من {total_count} — استخدم البحث أو التصنيف للوصول إلى كل الأصناف")
        except Exception as e: QMessageBox.critical(self, "خطأ", f"تعذر تحميل منتجات الكاشير:\n{e}")

    def add_search_result(self):
        text = self.cashier_search.text().strip()
        if not text: return
        try:
            with get_connection() as con:
                # Barcode scanners usually type the code and send Enter. Prefer exact barcode matches.
                exact_barcode = con.execute("SELECT id,code,COALESCE(barcode,'') FROM products WHERE stock_quantity>0 AND lower(COALESCE(barcode,''))=? LIMIT 1", (text.casefold(),)).fetchone()
                if exact_barcode:
                    rows = [exact_barcode]
                else:
                    pat = f"%{text}%"
                    rows = con.execute("SELECT id,code,COALESCE(barcode,'') FROM products WHERE stock_quantity>0 AND (lower(code)=? OR lower(name) LIKE ? OR lower(code) LIKE ? OR lower(COALESCE(barcode,'')) LIKE ?) ORDER BY CASE WHEN lower(code)=? THEN 0 ELSE 1 END, name LIMIT 2", (text.casefold(),pat,pat,pat,text.casefold())).fetchall()
            if not rows:
                QMessageBox.information(self, "لا توجد نتيجة", "لم يتم العثور على منتج متاح بهذا الاسم أو الكود أو الباركود."); return
            if len(rows) > 1 and text.casefold() not in (str(rows[0][1]).casefold(), str(rows[0][2]).casefold()):
                QMessageBox.information(self, "نتائج متعددة", "اكتب اسمًا أو كودًا أدق، أو اضغط مباشرةً على مربع المنتج المطلوب."); return
            self.add_product_to_cart(rows[0][0])
            self.cashier_search.clear(); self.cashier_search.setFocus()
        except Exception as e: QMessageBox.critical(self, "خطأ", f"تعذر البحث عن المنتج:\n{e}")

    def add_product_to_cart(self, product_id):
        try:
            with get_connection() as con:
                p = con.execute("SELECT id,code,name,COALESCE(size,''),COALESCE(color,''),selling_price,stock_quantity FROM products WHERE id=?", (product_id,)).fetchone()
            if not p: return
            if p[6] <= 0: QMessageBox.warning(self, "المخزون غير كافٍ", "المنتج غير متاح بالمخزون حاليًا."); return
            if product_id in self.cart:
                if self.cart[product_id]['quantity'] >= p[6]: QMessageBox.warning(self, "المخزون غير كافٍ", "الكمية المطلوبة أكبر من المخزون المتاح."); return
                self.cart[product_id]['quantity'] += 1
            else:
                self.cart[product_id] = {'id':p[0], 'code':p[1], 'name':p[2], 'size':p[3], 'color':p[4], 'price':p[5], 'quantity':1, 'stock':p[6]}
            self.render_cart(selected_pid=product_id); self.refresh_cashier_products()
        except Exception as e: QMessageBox.critical(self, "خطأ", f"تعذر إضافة المنتج:\n{e}")

    def render_cart(self, selected_pid=None):
        # Rebuilding the table normally clears selection; remember it and restore it.
        if selected_pid is None and hasattr(self, "cart_table"):
            old_row = self.cart_table.currentRow()
            if old_row >= 0 and self.cart_table.item(old_row, 0) is not None:
                try:
                    selected_pid = int(self.cart_table.item(old_row, 0).text())
                except (TypeError, ValueError):
                    selected_pid = None
        self.cart_table.setRowCount(len(self.cart))
        for r,(pid,p) in enumerate(self.cart.items()):
            variant=" / ".join(x for x in (p['size'],p['color']) if x) or "—"
            vals=[pid,p['name'],variant,money(p['price']),p['quantity'],money(p['price']*p['quantity'])]
            for c,v in enumerate(vals):
                item=QTableWidgetItem(str(v)); item.setTextAlignment(Qt.AlignmentFlag.AlignCenter); item.setToolTip(str(v)); self.cart_table.setItem(r,c,item)
        # If the selected product was removed, select nothing rather than a different product.
        if selected_pid in self.cart:
            for row in range(self.cart_table.rowCount()):
                cell = self.cart_table.item(row, 0)
                if cell is not None and int(cell.text()) == selected_pid:
                    self.cart_table.selectRow(row)
                    self.cart_table.setCurrentCell(row, 1)
                    break
        distinct_items = len(self.cart)
        total_units = sum(item['quantity'] for item in self.cart.values())
        if hasattr(self, "cart_summary_label"):
            self.cart_summary_label.setText(f"الأصناف: {distinct_items}  |  إجمالي القطع: {total_units}")
        self.cart_column_fitter.fit()
        self.update_cart_totals()

    def selected_cart_id(self):
        row=self.cart_table.currentRow()
        if row<0: return None
        return int(self.cart_table.item(row,0).text())

    def increase_cart_quantity(self):
        pid = self.selected_cart_id()
        if pid is None:
            QMessageBox.information(self, "اختيار صنف", "اختار صنفًا من الفاتورة أولًا."); return
        item = self.cart[pid]
        if item['quantity'] >= item['stock']:
            QMessageBox.warning(self, "المخزون غير كافٍ", "وصلت للكمية المتاحة في المخزون."); return
        item['quantity'] += 1
        self.render_cart(selected_pid=pid)

    def decrease_cart_quantity(self):
        pid = self.selected_cart_id()
        if pid is None:
            QMessageBox.information(self, "اختيار صنف", "اختار صنفًا من الفاتورة أولًا."); return
        item = self.cart[pid]
        if item['quantity'] <= 1:
            answer = QMessageBox.question(self, "حذف الصنف", "الكمية الحالية 1. هل تريد حذف الصنف من الفاتورة؟", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
            if answer == QMessageBox.StandardButton.Yes:
                del self.cart[pid]
        else:
            item['quantity'] -= 1
        self.render_cart(selected_pid=pid)

    def change_cart_quantity(self):
        pid=self.selected_cart_id()
        if pid is None: QMessageBox.information(self,"اختيار صنف","اختار صنفًا من الفاتورة أولًا."); return
        p=self.cart[pid]
        qty,ok=QInputDialog.getInt(self,"تعديل الكمية","الكمية المطلوبة:",p['quantity'],1,max(1,p['stock']),1)
        if ok: p['quantity']=qty; self.render_cart(selected_pid=pid)

    def remove_cart_item(self):
        pid=self.selected_cart_id()
        if pid is None: QMessageBox.information(self,"اختيار صنف","اختار صنفًا من الفاتورة أولًا."); return
        del self.cart[pid]; self.render_cart()

    def clear_cart(self):
        if not self.cart: return
        answer=QMessageBox.question(self,"تأكيد","هل تريد تفريغ الفاتورة الحالية؟")
        if answer==QMessageBox.StandardButton.Yes: self.cart.clear(); self.discount_input.setValue(0); self.paid_input.setValue(0); self.render_cart()

    def cart_subtotal(self): return sum(p['price']*p['quantity'] for p in self.cart.values())

    def update_cart_totals(self):
        if not hasattr(self,"change_label"): return  # change_label is the last summary widget built; totals are refreshed again once the page is complete
        subtotal=self.cart_subtotal(); discount=to_piasters(self.discount_input.value()); total=max(0,subtotal-discount); paid=to_piasters(self.paid_input.value())
        # Keep the UI summary aligned with checkout's actual discount rules.
        if self.current_user and self.current_user.get("role") != "admin":
            discount = 0
            total = subtotal
        self.subtotal_label.setText(f"الإجمالي قبل الخصم: {money(subtotal)}")
        self.discount_summary_label.setText(f"قيمة الخصم: {money(discount)}")
        self.total_label.setText(f"الإجمالي المطلوب: {money(total)}")
        method=self.payment_method.currentText()
        change=max(0,paid-total) if method==PAYMENT_CASH else 0
        due=max(0,total-min(paid,total)) if method==PAYMENT_SPLIT else (total if method==PAYMENT_CREDIT else 0)
        self.change_label.setText(f"الباقي للعميل: {money(change)}" + (f" | المتبقي آجلًا: {money(due)}" if due else ""))
        if hasattr(self, "checkout_button"):
            self.checkout_button.setEnabled(bool(self.cart))

    def confirm_new_debtor(self, name):
        """Explicit confirmation before a NEW debtor account is created by a credit/split sale.

        Shows the customer details first and warns when the name already exists.  Returns the
        ``new_customer`` dict for ``create_sale`` or ``None`` when the operator declines.
        """
        try:
            with get_connection() as con:
                same=find_customers_by_name(con,name)
        except Exception:
            self.logger.exception("Unable to check existing customers")
            QMessageBox.critical(self,"تعذر التحقق","تعذر التحقق من العملاء المسجلين. لم يتم إنشاء أي عميل."); return None
        text=f"سيتم إنشاء حساب عميل جديد:\n\nالاسم: {name}\nالهاتف: غير مسجل\nالرصيد الافتتاحي: 0.00\n"
        if same:
            listing="\n".join(f"• {c['name']} — {c['phone'] or 'بدون هاتف'}" for c in same)
            text+=f"\n⚠ يوجد بالفعل عميل بنفس الاسم:\n{listing}\nإذا كان هو نفسه فاختره من القائمة بدل إنشاء حساب جديد.\n"
        text+="\nهل تريد إنشاء الحساب الجديد وتسجيل المديونية عليه؟"
        answer=QMessageBox.question(self,"تأكيد إنشاء عميل جديد",text,
            QMessageBox.StandardButton.Yes|QMessageBox.StandardButton.No,QMessageBox.StandardButton.No)
        if answer!=QMessageBox.StandardButton.Yes:
            return None
        return {"name":name,"phone":"","allow_duplicate":bool(same)}

    def checkout(self):
        if not self.cart:
            QMessageBox.information(self,"فاتورة فارغة","أضف منتجًا واحدًا على الأقل للفاتورة."); return
        method=self.payment_method.currentText()
        discount=to_piasters(self.discount_input.value()) if self.current_user["role"]=="admin" else 0
        cash_received=0 if method==PAYMENT_CREDIT else to_piasters(self.paid_input.value())
        customer_text=self.customer_combo.currentText().strip()
        # Identity is the customer id.  An existing customer is only ever attached when it is selected
        # from the list (labels are unique); a typed name alone never attaches or creates anyone.
        selected_customer_id=None
        new_customer=None
        combo_index=self.customer_combo.currentIndex()
        if combo_index > 0 and self.customer_combo.itemText(combo_index).strip().casefold()==customer_text.casefold():
            selected_customer_id=self.customer_combo.itemData(combo_index)
        elif method!=PAYMENT_CASH:
            if not customer_text:
                QMessageBox.warning(self,"عميل مطلوب","البيع الآجل أو المختلط يتطلب اختيار عميل مسجل من القائمة، أو كتابة اسم عميل جديد وتأكيد إنشائه."); return
            new_customer=self.confirm_new_debtor(customer_text)
            if new_customer is None: return
        # (a cash sale with an unknown typed name simply has no customer: nothing is created)
        cart_lines=[{"product_id":p["id"],"quantity":p["quantity"],"unit_price":p["price"]} for p in self.cart.values()]
        if not getattr(self, "_checkout_request_id", None):
            self._checkout_request_id = uuid.uuid4().hex      # one key per checkout attempt; retries are safe
        try:
            with get_connection() as con:
                result=create_sale(con,user=self.current_user,cart_lines=cart_lines,
                    discount=discount,payment_method=method,cash_received=cash_received,
                    request_id=self._checkout_request_id,
                    selected_customer_id=selected_customer_id,new_customer=new_customer)
            self._checkout_request_id = None
            message=(("هذه الفاتورة محفوظة بالفعل (تم منع التكرار).\n" if result.get("duplicate") else "")+f"تم حفظ الفاتورة بنجاح.\nرقم الفاتورة: {result['invoice_number']}\n"
                     f"صافي الفاتورة: {money(result['total'])}\n"
                     f"المبلغ النقدي: {money(result['cash_amount'])}\n"
                     f"المتبقي على العميل: {money(result['credit_amount'])}\n"
                     f"الباقي النقدي: {money(result['change'])}")
            QMessageBox.information(self,"تم البيع",message)
            self.cart.clear(); self.discount_input.setValue(0); self.paid_input.setValue(0); self.customer_combo.setCurrentText("")
            self.payment_method.setCurrentText(PAYMENT_CASH)
            self.render_cart(); self.refresh_cashier_products(); self.refresh_customer_combo()
        except Exception as exc:
            self.logger.exception("Checkout failed")
            QMessageBox.critical(self,"تعذر حفظ الفاتورة",f"لم يتم إتمام البيع أو خصم المخزون.\n\nالتفاصيل: {exc}")

    def create_shifts_page(self):
        page = QWidget(); layout = QVBoxLayout(page); layout.setSpacing(14)
        title = QLabel("الورديات والخزنة"); title.setObjectName("PageTitle"); layout.addWidget(title)
        self.shift_status_label = QLabel("جاري تحميل حالة الوردية..."); self.shift_status_label.setWordWrap(True)
        self.shift_status_label.setStyleSheet(T.qss("background:@surface@;border:1px solid @border_soft@;border-radius:10px;padding:14px;font-size:16px;"))
        layout.addWidget(self.shift_status_label)
        actions = QHBoxLayout()
        self.open_shift_btn = QPushButton("فتح وردية جديدة"); self.open_shift_btn.setObjectName("Primary"); self.open_shift_btn.clicked.connect(self.open_cash_shift); actions.addWidget(self.open_shift_btn)
        self.movement_btn = QPushButton("تسجيل حركة خزنة"); self.movement_btn.setObjectName("Primary"); self.movement_btn.clicked.connect(self.add_cash_movement); actions.addWidget(self.movement_btn)
        self.close_shift_btn = QPushButton("إغلاق الوردية"); self.close_shift_btn.setObjectName("Danger"); self.close_shift_btn.clicked.connect(self.close_cash_shift); actions.addWidget(self.close_shift_btn)
        refresh = QPushButton("تحديث"); refresh.clicked.connect(self.refresh_shifts_page); actions.addWidget(refresh)
        layout.addLayout(actions)
        subtitle = QLabel("سجل الورديات السابقة"); subtitle.setStyleSheet("font-size:18px;font-weight:bold;margin-top:8px;"); layout.addWidget(subtitle)
        self.shifts_table = QTableWidget(0, 9)
        self.shifts_table.setHorizontalHeaderLabels(["رقم", "المستخدم", "فتح الوردية", "إغلاق الوردية", "بداية الخزنة", "المتوقع", "الفعلي", "الفرق", "مرتجعات غير مصنفة"])
        self.shifts_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.shifts_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.shifts_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.shifts_table.verticalHeader().setVisible(False); layout.addWidget(self.shifts_table, 1)
        movement_title = QLabel("سجل حركات الخزنة (مثل مصروف الشاي والمشتريات الصغيرة)")
        movement_title.setStyleSheet("font-size:18px;font-weight:bold;margin-top:8px;")
        layout.addWidget(movement_title)
        self.cash_movements_table = QTableWidget(0, 6)
        self.cash_movements_table.setHorizontalHeaderLabels(["التاريخ", "الوردية", "المستخدم", "النوع", "المبلغ", "السبب / الملاحظات"])
        self.cash_movements_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.cash_movements_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.cash_movements_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.cash_movements_table.verticalHeader().setVisible(False)
        self.cash_movements_table.setMaximumHeight(220)
        layout.addWidget(self.cash_movements_table)
        return page

    def get_open_shift(self, con=None):
        own = con is None
        if own: con = get_connection()
        try:
            return con.execute("SELECT id,user_id,opened_at,opening_cash FROM cash_shifts WHERE status='open' AND user_id=? ORDER BY id DESC LIMIT 1", (self.current_user["id"],)).fetchone()
        finally:
            if own: con.close()

    def refresh_shifts_page(self):
        if not hasattr(self, "shifts_table"): return
        try:
            with get_connection() as con:
                shift = self.get_open_shift(con)
                if shift:
                    expected = self.calculate_expected_cash(shift[0], con)
                    user = con.execute("SELECT display_name FROM users WHERE id=?", (shift[1],)).fetchone()
                    unknown_refunds = unclassified_refunds_for_shift(con, shift[0])
                    warning = (f"\nتنبيه: مرتجعات تاريخية بقيمة {money(unknown_refunds)} لا نعرف هل رُدت نقدًا أم كرصيد؛ لم تُخصم من المتوقع حتى لا نفترض طريقة رد غير مثبتة.") if unknown_refunds else ""
                    self.shift_status_label.setText(f"الوردية المفتوحة رقم #{shift[0]} — المسؤول: {user[0] if user else 'غير معروف'}\nبدأت: {shift[2]}\nبداية الخزنة: {money(shift[3])} | الرصيد المتوقع حاليًا: {money(expected)}{warning}")
                else:
                    self.shift_status_label.setText("لا توجد وردية مفتوحة حاليًا. افتح وردية وحدد مبلغ بداية الخزنة قبل بدء البيع.")
                self.open_shift_btn.setEnabled(shift is None)
                self.movement_btn.setEnabled(shift is not None)
                self.close_shift_btn.setEnabled(shift is not None)
                if self.current_user["role"] == "admin":
                    rows = con.execute("SELECT s.id,COALESCE(u.display_name,''),s.opened_at,COALESCE(s.closed_at,'—'),s.opening_cash,s.expected_cash,s.actual_cash,s.difference,COALESCE((SELECT SUM(sr.total_refund) FROM sales_returns sr LEFT JOIN invoices i ON i.id=sr.invoice_id WHERE sr.refund_method='unknown' AND COALESCE(sr.shift_id,i.shift_id)=s.id),0) FROM cash_shifts s LEFT JOIN users u ON u.id=s.user_id ORDER BY s.id DESC LIMIT 200").fetchall()
                else:
                    rows = con.execute("SELECT s.id,COALESCE(u.display_name,''),s.opened_at,COALESCE(s.closed_at,'—'),s.opening_cash,s.expected_cash,s.actual_cash,s.difference,COALESCE((SELECT SUM(sr.total_refund) FROM sales_returns sr LEFT JOIN invoices i ON i.id=sr.invoice_id WHERE sr.refund_method='unknown' AND COALESCE(sr.shift_id,i.shift_id)=s.id),0) FROM cash_shifts s LEFT JOIN users u ON u.id=s.user_id WHERE s.user_id=? ORDER BY s.id DESC LIMIT 200", (self.current_user["id"],)).fetchall()
                if self.current_user["role"] == "admin":
                    movement_rows = con.execute("SELECT m.created_at,m.shift_id,COALESCE(u.display_name,''),m.movement_type,m.amount,m.reason FROM cash_movements m LEFT JOIN users u ON u.id=m.user_id ORDER BY m.id DESC LIMIT 200").fetchall()
                else:
                    movement_rows = con.execute("SELECT m.created_at,m.shift_id,COALESCE(u.display_name,''),m.movement_type,m.amount,m.reason FROM cash_movements m LEFT JOIN users u ON u.id=m.user_id WHERE m.user_id=? ORDER BY m.id DESC LIMIT 200", (self.current_user["id"],)).fetchall()
            self.shifts_table.setRowCount(len(rows))
            for r, row in enumerate(rows):
                values = [row[0], row[1], row[2], row[3], money(row[4]), money(row[5]) if row[5] is not None else "—", money(row[6]) if row[6] is not None else "—", money(row[7]) if row[7] is not None else "—", money(row[8]) if row[8] else "0.00 جنيه"]
                for c, value in enumerate(values):
                    item = QTableWidgetItem(str(value)); item.setTextAlignment(Qt.AlignmentFlag.AlignCenter); self.shifts_table.setItem(r, c, item)
            self.cash_movements_table.setRowCount(len(movement_rows))
            for r, move in enumerate(movement_rows):
                values = [move[0], move[1], move[2], move[3], money(move[4]), move[5] or "—"]
                for c, value in enumerate(values):
                    item = QTableWidgetItem(str(value)); item.setTextAlignment(Qt.AlignmentFlag.AlignCenter); self.cash_movements_table.setItem(r, c, item)
            if hasattr(self, "cashier_shift_button"):
                self.cashier_shift_button.setText("✓ الوردية مفتوحة — إدارة الخزنة" if shift else "🕒 افتح ورديتك قبل البيع")
                self.cashier_shift_button.setStyleSheet(T.qss("background:@success@;color:@text_on_deep@;border-radius:8px;padding:10px;font-weight:bold;") if shift else T.qss("background:@accent_alt@;color:@text_on_accent@;border-radius:8px;padding:10px;font-weight:bold;"))
        except Exception as e:
            QMessageBox.critical(self, "خطأ", f"تعذر تحميل الورديات وحركات الخزنة:\n{e}")

    def calculate_expected_cash(self, shift_id, con):
        return calculate_expected_cash(con, shift_id)

    def open_cash_shift(self):
        with get_connection() as con:
            if self.get_open_shift(con):
                QMessageBox.warning(self, "هناك وردية مفتوحة", "أغلق الوردية الحالية أولًا قبل فتح وردية جديدة."); self.refresh_shifts_page(); return
        amount, ok = QInputDialog.getDouble(self, "فتح وردية", "مبلغ بداية الخزنة بالجنيه:", 0.0, 0.0, 100000000.0, 2)
        if not ok: return
        try:
            with get_connection() as con:
                service_open_shift(con, user=self.current_user, opening_cash=to_piasters(amount))
            QMessageBox.information(self, "تم فتح الوردية", f"تم فتح الوردية بمبلغ {money(to_piasters(amount))}.")
            self.refresh_shifts_page()
            self.show_cashier() if self.pages.currentWidget() == self.cashier_page else None
        except Exception as e: QMessageBox.critical(self, "تعذر فتح الوردية", str(e))

    def add_cash_movement(self):
        shift = self.get_open_shift()
        if not shift:
            QMessageBox.warning(self, "لا توجد وردية", "افتح وردية أولًا."); return
        dialog = QDialog(self); dialog.setWindowTitle("تسجيل حركة خزنة"); dialog.setMinimumWidth(400); dialog.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        form = QFormLayout(dialog)
        kind = QComboBox(); kind.addItems(["مصروف", "سحب", "إيداع"])
        amount = QDoubleSpinBox(); amount.setRange(0.01, 100000000); amount.setDecimals(2); amount.setSuffix(" جنيه")
        reason = QLineEdit(); reason.setPlaceholderText("مثال: شراء مستلزمات أو إيداع نقدية")
        form.addRow("نوع الحركة:", kind); form.addRow("المبلغ:", amount); form.addRow("السبب:", reason)
        buttons = QHBoxLayout(); save = QPushButton("حفظ الحركة"); save.setObjectName("Primary"); cancel = QPushButton("إلغاء"); buttons.addWidget(save); buttons.addWidget(cancel); form.addRow(buttons)
        save.clicked.connect(dialog.accept); cancel.clicked.connect(dialog.reject)
        if dialog.exec() != QDialog.DialogCode.Accepted: return
        if not reason.text().strip(): QMessageBox.warning(self, "السبب مطلوب", "اكتب سبب حركة الخزنة."); return
        try:
            with get_connection() as con:
                service_add_cash_movement(con, user=self.current_user, shift_id=shift[0], movement_type=kind.currentText(),
                                          amount=to_piasters(amount.value()), reason=reason.text().strip())
            QMessageBox.information(self, "تم الحفظ", "تم تسجيل حركة الخزنة."); self.refresh_shifts_page()
        except Exception as e: QMessageBox.critical(self, "خطأ", f"تعذر تسجيل الحركة:\n{e}")

    def close_cash_shift(self):
        shift = self.get_open_shift()
        if not shift: QMessageBox.information(self, "لا توجد وردية", "لا توجد وردية مفتوحة لإغلاقها."); return
        with get_connection() as con:
            expected = self.calculate_expected_cash(shift[0], con)
            unknown_refunds = unclassified_refunds_for_shift(con, shift[0])
        close_context = f"\nمرتجعات تاريخية غير مصنفة: {money(unknown_refunds)} (مستبعدة من الرصيد المتوقع)" if unknown_refunds else ""
        # Blind count for cashiers: the expected amount is NOT shown (nor pre-filled) before the count is entered.
        blind_count = (self.current_user or {}).get("role") != "admin"
        if blind_count:
            prompt = f"عدّ النقدية الموجودة فعليًا في الخزنة واكتب المبلغ بالجنيه:{close_context}"
            default_value = 0.0
        else:
            prompt = f"الرصيد المتوقع: {money(expected)}{close_context}\nاكتب المبلغ الفعلي الموجود في الخزنة بالجنيه:"
            default_value = expected/100
        actual, ok = QInputDialog.getDouble(self, "إغلاق الوردية", prompt, default_value, 0.0, 100000000.0, 2)
        if not ok: return
        actual_p = to_piasters(actual); difference = actual_p - expected
        answer = QMessageBox.question(self, "تأكيد إغلاق الوردية", f"الرصيد المتوقع: {money(expected)}\nالرصيد الفعلي: {money(actual_p)}\nالفرق: {money(difference)}{close_context}\n\nهل راجعت التنبيه وتريد إغلاق الوردية؟")
        if answer != QMessageBox.StandardButton.Yes: return
        try:
            with get_connection() as con:
                # The expected figure is compared INSIDE the service transaction; if it changed, nothing is closed.
                closed = service_close_shift(con, user=self.current_user, shift_id=shift[0], actual_cash=actual_p,
                                             expected_cash=expected)
                latest_expected = closed["expected"]
            QMessageBox.information(self, "تم إغلاق الوردية", f"تم إغلاق الوردية.\nالفرق: {money(actual_p-latest_expected)}")
            self.refresh_shifts_page()
            self.show_cashier() if self.pages.currentWidget() == self.cashier_page else None
        except Exception as e: QMessageBox.critical(self, "خطأ", f"تعذر إغلاق الوردية:\n{e}")

    def create_invoices_page(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        header = QHBoxLayout()
        title = QLabel("الفواتير المسجلة")
        title.setObjectName("PageTitle")
        header.addWidget(title)
        header.addStretch()
        details_btn = QPushButton("عرض تفاصيل الفاتورة المحددة")
        details_btn.setObjectName("Primary")
        details_btn.clicked.connect(self.show_selected_invoice_details)
        header.addWidget(details_btn)
        return_btn = QPushButton("إنشاء مرتجع")
        return_btn.setObjectName("Primary")
        return_btn.clicked.connect(self.open_sales_return_dialog)
        header.addWidget(return_btn)
        returns_history_btn = QPushButton("سجل المرتجعات")
        returns_history_btn.setObjectName("Primary")
        returns_history_btn.clicked.connect(self.show_sales_returns_history)
        header.addWidget(returns_history_btn)
        refresh_btn = QPushButton("تحديث")
        refresh_btn.setObjectName("Primary")
        refresh_btn.clicked.connect(self.load_invoices)
        header.addWidget(refresh_btn)
        layout.addLayout(header)

        search_row = QHBoxLayout()
        self.invoice_search_input = QLineEdit()
        self.invoice_search_input.setPlaceholderText("ابحث برقم الفاتورة...")
        self.invoice_search_input.setClearButtonEnabled(True)
        self.invoice_search_input.textChanged.connect(self.load_invoices)
        search_row.addWidget(self.invoice_search_input, 2)
        search_row.addWidget(QLabel("تاريخ اليوم التجاري:"))
        self.invoice_search_date = QDateEdit()
        self.invoice_search_date.setCalendarPopup(True)
        self.invoice_search_date.setDisplayFormat("yyyy-MM-dd")
        self.invoice_search_date.setDate(QDate.fromString(business_date_for(datetime.now()), "yyyy-MM-dd"))
        self.invoice_search_date.setSpecialValueText("كل التواريخ")
        self.invoice_search_date.setMinimumDate(QDate(2000, 1, 1))
        self.invoice_search_date.setMaximumDate(QDate(2099, 12, 31))
        self.invoice_search_date_check = QCheckBox("تحديد تاريخ")
        self.invoice_search_date_check.setChecked(False)
        self.invoice_search_date_check.toggled.connect(self.invoice_search_date.setEnabled)
        self.invoice_search_date_check.toggled.connect(self.load_invoices)
        self.invoice_search_date.dateChanged.connect(self.load_invoices)
        self.invoice_search_date.setEnabled(False)
        search_row.addWidget(self.invoice_search_date_check)
        search_row.addWidget(self.invoice_search_date)
        layout.addLayout(search_row)

        self.invoices_table = QTableWidget(0, 7)
        self.invoices_table.setHorizontalHeaderLabels(
            ["ID", "رقم الفاتورة", "التاريخ", "قبل الخصم", "الخصم", "الصافي", "طريقة الدفع"]
        )
        self.invoices_table.setColumnHidden(0, True)
        self.invoices_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.invoices_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.invoices_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.invoices_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.invoices_table.verticalHeader().setVisible(False)
        self.invoices_table.cellDoubleClicked.connect(lambda *_: self.show_selected_invoice_details())
        layout.addWidget(self.invoices_table)
        note = QLabel("اضغط مرتين على الفاتورة أو اخترها ثم اضغط عرض التفاصيل.")
        note.setObjectName("Muted")
        layout.addWidget(note)
        return page

    def load_invoices(self):
        if not self.require_admin(): return
        if not hasattr(self, "invoices_table"):
            return
        try:
            search_text = self.invoice_search_input.text().strip() if hasattr(self, "invoice_search_input") else ""
            sql = ("SELECT id, COALESCE(daily_number, invoice_number), created_at, subtotal, discount, total, payment_method "
                   "FROM invoices WHERE 1=1")
            params = []
            if search_text:
                sql += " AND (COALESCE(daily_number,'') LIKE ? OR invoice_number LIKE ?)"
                params.extend([f"%{search_text}%", f"%{search_text}%"])
            if hasattr(self, "invoice_search_date_check") and self.invoice_search_date_check.isChecked():
                sql += " AND business_date = ?"
                params.append(self.invoice_search_date.date().toString("yyyy-MM-dd"))
            sql += " ORDER BY id DESC"
            with get_connection() as con:
                rows = con.execute(sql, params).fetchall()
            self.invoices_table.setRowCount(len(rows))
            for r, row in enumerate(rows):
                vals = [row[0], row[1], row[2], money(row[3]), money(row[4]), money(row[5]), row[6]]
                for c, value in enumerate(vals):
                    item = QTableWidgetItem(str(value))
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    self.invoices_table.setItem(r, c, item)
        except Exception as e:
            QMessageBox.critical(self, "خطأ", f"تعذر تحميل الفواتير:\n{e}")

    def show_selected_invoice_details(self):
        if not self.require_admin(): return
        row = self.invoices_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "اختيار فاتورة", "اختار فاتورة من الجدول أولًا.")
            return
        self.show_invoice_details(int(self.invoices_table.item(row, 0).text()))

    def show_invoice_details(self, invoice_id):
        try:
            with get_connection() as con:
                invoice = con.execute(
                    "SELECT COALESCE(i.daily_number, i.invoice_number),i.created_at,i.subtotal,i.discount,i.total,i.payment_method,i.amount_paid,i.change_amount,COALESCE(i.credit_amount,0),COALESCE(c.name,'') "
                    "FROM invoices i LEFT JOIN customers c ON c.id=i.customer_id WHERE i.id=?", (invoice_id,)
                ).fetchone()
                items = con.execute(
                    "SELECT product_code, product_name, product_size, product_color, quantity, unit_price, line_total "
                    "FROM invoice_items WHERE invoice_id=? ORDER BY id", (invoice_id,)
                ).fetchall()
            if not invoice:
                QMessageBox.warning(self, "فاتورة غير موجودة", "لم يتم العثور على الفاتورة.")
                return
            dialog = QDialog(self)
            dialog.setWindowTitle(f"تفاصيل الفاتورة {invoice[0]}")
            dialog.resize(850, 520)
            dialog.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
            layout = QVBoxLayout(dialog)
            info = QLabel(
                f"رقم الفاتورة: {invoice[0]}    |    التاريخ: {invoice[1]}\n"
                f"طريقة الدفع: {invoice[5]}    |    المدفوع: {money(invoice[6])}    |    الباقي: {money(invoice[7])}\n"
                f"قبل الخصم: {money(invoice[2])}    |    الخصم: {money(invoice[3])}    |    الصافي: {money(invoice[4])}"
            )
            info.setWordWrap(True)
            info.setStyleSheet("font-size:15px;font-weight:bold;padding:10px;")
            layout.addWidget(info)
            table = QTableWidget(0, 7)
            table.setHorizontalHeaderLabels(["الكود", "المنتج", "المقاس", "اللون", "الكمية", "سعر الوحدة", "الإجمالي"])
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            table.verticalHeader().setVisible(False)
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.setRowCount(len(items))
            for r, item_row in enumerate(items):
                vals = [item_row[0], item_row[1], item_row[2] or "—", item_row[3] or "—",
                        item_row[4], money(item_row[5]), money(item_row[6])]
                for c, value in enumerate(vals):
                    cell = QTableWidgetItem(str(value))
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    table.setItem(r, c, cell)
            layout.addWidget(table)
            actions = QHBoxLayout()
            print_btn = QPushButton("طباعة الفاتورة")
            print_btn.setObjectName("Primary")
            print_btn.clicked.connect(lambda: self.print_invoice(invoice_id))
            pdf_btn = QPushButton("حفظ كـ PDF")
            pdf_btn.clicked.connect(lambda: self.save_invoice_pdf(invoice_id))
            close_btn = QPushButton("إغلاق")
            close_btn.clicked.connect(dialog.accept)
            actions.addWidget(print_btn)
            actions.addWidget(pdf_btn)
            actions.addStretch()
            actions.addWidget(close_btn)
            layout.addLayout(actions)
            dialog.exec()
        except Exception as e:
            QMessageBox.critical(self, "خطأ", f"تعذر فتح تفاصيل الفاتورة:\n{e}")

    def _invoice_receipt_html(self, invoice_id):
        # Compact centered 80mm thermal receipt; stored invoice data stays unchanged.
        with get_connection() as con:
            invoice = con.execute(
                "SELECT COALESCE(i.daily_number,i.invoice_number),i.created_at,i.subtotal,i.discount,i.total,i.payment_method,i.amount_paid,i.change_amount,COALESCE(i.credit_amount,0),COALESCE(c.name,'') "
                "FROM invoices i LEFT JOIN customers c ON c.id=i.customer_id WHERE i.id=?", (invoice_id,)
            ).fetchone()
            items = con.execute(
                "SELECT product_name, product_size, product_color, quantity, unit_price, line_total "
                "FROM invoice_items WHERE invoice_id=? ORDER BY id", (invoice_id,)
            ).fetchall()
        if not invoice:
            raise ValueError("الفاتورة غير موجودة")
        safe = lambda value: html.escape(str(value if value is not None else "—"))
        item_rows = "".join(
            "<tr>"
            + f"<td class='product'>{safe(name)}"
            + (f"<small>المقاس: {safe(size)}</small>" if size else "")
            + (f"<small>اللون: {safe(color)}</small>" if color else "")
            + "</td>"
            + f"<td>{int(qty)}</td><td>{unit_price / 100:.2f}</td><td>{line_total / 100:.2f}</td></tr>"
            for name, size, color, qty, unit_price, line_total in items
        )
        return f'''<!DOCTYPE html><html lang="ar"><head><meta charset="utf-8"><style>
        @page {{ margin: 0; }}
        body {{ font-family: Tahoma, Arial, sans-serif; direction: rtl; color:#000; font-size:8pt;
                width:76mm; max-width:76mm; margin:0 auto; padding:0; line-height:1.4; }}
        .receipt {{ width:100%; margin:0 auto; text-align:center; }}
        h2 {{ font-size:14pt; margin:0 0 2mm; text-align:center; }}
        .center {{ text-align:center; }}
        .meta {{ text-align:right; margin:3mm 0; overflow-wrap:anywhere; }}
        .rule {{ border-top:1px dashed #000; margin:2mm 0; height:0; }}
        table {{ width:100%; border-collapse:collapse; table-layout:fixed; margin:2mm 0; font-size:7.5pt; }}
        th,td {{ border-bottom:1px solid #777; padding:2mm 0.5mm; text-align:center; overflow-wrap:anywhere; }}
        th {{ font-weight:bold; }}
        th:first-child,td:first-child {{ width:47%; text-align:right; }}
        th:nth-child(2),td:nth-child(2) {{ width:11%; }}
        th:nth-child(3),td:nth-child(3) {{ width:20%; }}
        th:nth-child(4),td:nth-child(4) {{ width:22%; }}
        .product small {{ display:block; font-size:6.5pt; }}
        .totals {{ text-align:right; line-height:1.8; margin-top:3mm; }}
        .grand {{ font-size:10pt; font-weight:bold; }}
        .thanks {{ text-align:center; margin-top:5mm; }}
        </style></head><body><div class="receipt">
        <h2>Smart POS</h2><div class="center">فاتورة بيع</div><div class="rule"></div>
        <div class="meta">رقم الفاتورة: <b style="font-size:11pt">{safe(invoice[0])}</b><br>تاريخ ووقت البيع: <b>{safe(invoice[1])}</b><br>طريقة الدفع: {safe(invoice[5])}</div>
        <table><thead><tr><th>المنتج</th><th>الكمية</th><th>سعر الوحدة</th><th>الإجمالي</th></tr></thead><tbody>{item_rows}</tbody></table>
        <div class="totals">الإجمالي قبل الخصم: {invoice[2]/100:.2f} جنيه<br>الخصم: {invoice[3]/100:.2f} جنيه<br>
        <span class="grand">الصافي: {invoice[4]/100:.2f} جنيه</span><br>المدفوع: {invoice[6]/100:.2f} جنيه<br>الباقي: {invoice[7]/100:.2f} جنيه</div>
        <div class="rule"></div><div class="thanks">شكرًا لزيارتكم</div></div></body></html>'''

    def print_invoice(self, invoice_id):
        try:
            document = QTextDocument()
            document.setHtml(self._invoice_receipt_html(invoice_id))
            printer = QPrinter(QPrinter.PrinterMode.HighResolution)
            dialog = QPrintDialog(printer, self)
            dialog.setWindowTitle("طباعة الفاتورة - ورق حراري 80 مم")
            if dialog.exec() == QPrintDialog.DialogCode.Accepted:
                document.print(printer)
        except Exception as e:
            QMessageBox.critical(self, "خطأ في الطباعة", f"تعذرت طباعة الفاتورة:\n{e}")

    def save_invoice_pdf(self, invoice_id):
        try:
            with get_connection() as con:
                row = con.execute("SELECT COALESCE(daily_number, invoice_number) FROM invoices WHERE id=?", (invoice_id,)).fetchone()
                item_count = con.execute("SELECT COUNT(*) FROM invoice_items WHERE invoice_id=?", (invoice_id,)).fetchone()[0]
            if not row:
                QMessageBox.warning(self, "فاتورة غير موجودة", "لم يتم العثور على الفاتورة.")
                return
            path, _ = QFileDialog.getSaveFileName(self, "حفظ الفاتورة PDF (مقاس حراري 80 مم)", f"فاتورة_{row[0]}.pdf", "PDF (*.pdf)")
            if not path:
                return
            if not path.lower().endswith(".pdf"):
                path += ".pdf"
            # Narrow, tall page similar to an 80mm thermal receipt instead of A4.
            page_height_mm = max(100, 75 + int(item_count) * 13)
            pdf_writer = QPdfWriter(path)
            pdf_writer.setResolution(203)
            page_size = QPageSize(QSizeF(80, page_height_mm), QPageSize.Unit.Millimeter,
                                  "SmartPOS Thermal Receipt", QPageSize.SizeMatchPolicy.ExactMatch)
            pdf_writer.setPageSize(page_size)
            pdf_writer.setPageMargins(QMarginsF(2, 2, 2, 2), QPageLayout.Unit.Millimeter)
            document = QTextDocument()
            document.setDocumentMargin(0)
            document.setHtml(self._invoice_receipt_html(invoice_id))
            document.setPageSize(QSizeF(76 * 96 / 25.4, page_height_mm * 96 / 25.4))
            document.print_(pdf_writer)
            del document
            del pdf_writer
            if not os.path.isfile(path) or os.path.getsize(path) == 0:
                raise OSError("لم يتم إنشاء ملف PDF. اختر مجلدًا يمكن الكتابة فيه مثل سطح المكتب.")
            QMessageBox.information(self, "تم الحفظ", f"تم حفظ الفاتورة بالمقاس الحراري 80 مم:\n{path}")
        except Exception as e:
            QMessageBox.critical(self, "خطأ في PDF", f"تعذر حفظ الفاتورة:\n{e}")

    def open_sales_return_dialog(self):
        if not self.require_admin(): return
        row = self.invoices_table.currentRow()
        if row < 0:
            QMessageBox.information(self, "اختيار فاتورة", "اختار فاتورة البيع التي تريد إرجاع صنف منها أولًا.")
            return
        invoice_id = int(self.invoices_table.item(row, 0).text())
        try:
            with get_connection() as con:
                invoice = con.execute(
                    "SELECT COALESCE(daily_number, invoice_number), created_at, subtotal, total, customer_id FROM invoices WHERE id=?", (invoice_id,)
                ).fetchone()
                lines = con.execute("""
                    SELECT ii.id, ii.product_id, ii.product_code, ii.product_name,
                           ii.product_size, ii.product_color, ii.quantity, ii.unit_price, ii.line_total,
                           COALESCE((SELECT SUM(sri.quantity)
                                     FROM sales_return_items sri
                                     JOIN sales_returns sr ON sr.id=sri.return_id
                                     WHERE sri.invoice_item_id=ii.id), 0) AS returned_qty,
                           COALESCE((SELECT SUM(sri.line_refund)
                                     FROM sales_return_items sri
                                     WHERE sri.invoice_item_id=ii.id), 0) AS returned_value
                    FROM invoice_items ii WHERE ii.invoice_id=? ORDER BY ii.id
                """, (invoice_id,)).fetchall()
            if not invoice or not lines:
                QMessageBox.warning(self, "لا توجد أصناف", "لم يتم العثور على أصناف في الفاتورة المحددة.")
                return
            net_line_values = allocate_net_line_values(
                [(int(line[0]), int(line[8])) for line in lines], int(invoice[3]), int(invoice[2])
            )
            available_lines = [line for line in lines if line[6] - line[9] > 0]
            if not available_lines:
                QMessageBox.information(self, "تم إرجاع الفاتورة", "كل كميات هذه الفاتورة تم إرجاعها بالفعل.")
                return

            dialog = QDialog(self)
            dialog.setWindowTitle("إنشاء مرتجع بيع")
            dialog.resize(950, 560)
            dialog.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
            layout = QVBoxLayout(dialog)
            summary = QLabel(
                f"الفاتورة الأصلية: {invoice[0]}    |    التاريخ: {invoice[1]}\n"
                f"صافي الفاتورة بعد الخصم: {money(invoice[3])}\n"
                "حدد الكمية المطلوب إرجاعها. الكمية المتاحة للمرتجع تظهر في الجدول."
            )
            summary.setWordWrap(True)
            summary.setStyleSheet("font-weight:bold;padding:8px;")
            layout.addWidget(summary)
            table = QTableWidget(0, 8)
            table.setHorizontalHeaderLabels(["كود الصنف", "الصنف", "المقاس", "اللون", "الكمية المباعة", "مرتجع سابقًا", "متاح للإرجاع", "كمية الإرجاع"])
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            table.verticalHeader().setVisible(False)
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            table.setRowCount(len(available_lines))
            spinboxes = {}
            for r, line in enumerate(available_lines):
                iid, product_id, code, name, size, color, sold_qty, unit_price, line_total, returned_qty, returned_value = line
                available = sold_qty - returned_qty
                values = [code, name, size or "—", color or "—", sold_qty, returned_qty, available]
                for c, value in enumerate(values):
                    cell = QTableWidgetItem(str(value))
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    table.setItem(r, c, cell)
                spin = QSpinBox()
                spin.setRange(0, available)
                spin.setValue(0)
                spin.setAlignment(Qt.AlignmentFlag.AlignCenter)
                table.setCellWidget(r, 7, spin)
                spinboxes[iid] = (spin, line, net_line_values.get(int(iid), 0))
            layout.addWidget(table)
            total_label = QLabel("إجمالي المبلغ المسترد: 0.00 جنيه")
            total_label.setStyleSheet("font-size:17px;font-weight:bold;padding:8px;")
            layout.addWidget(total_label)
            refund_method = QComboBox(); refund_method.addItems(["نقدي", "رصيد العميل"])
            if not invoice[4]:
                refund_method.clear(); refund_method.addItem("نقدي")
                refund_method.setToolTip("الفاتورة غير مرتبطة بحساب عميل؛ يمكن رد المبلغ نقدًا فقط")
            layout.addWidget(QLabel("طريقة رد قيمة المرتجع:")); layout.addWidget(refund_method)

            def calculate_refund():
                amount = 0
                for spin, line, net_line_value in spinboxes.values():
                    if spin.value() <= 0:
                        continue
                    sold_qty, already_qty, already_value = int(line[6]), int(line[9]), int(line[10])
                    amount += calculate_line_return_refund(
                        int(net_line_value), sold_qty, already_qty, already_value, spin.value()
                    )
                total_label.setText(f"إجمالي المبلغ المسترد: {money(amount)}")
                return amount

            for spin, _line, _net_value in spinboxes.values():
                spin.valueChanged.connect(calculate_refund)
            buttons = QHBoxLayout()
            save_btn = QPushButton("تأكيد المرتجع وإعادة الكمية للمخزون")
            save_btn.setObjectName("Primary")
            cancel_btn = QPushButton("إلغاء")
            buttons.addWidget(save_btn)
            buttons.addWidget(cancel_btn)
            layout.addLayout(buttons)

            def save_return():
                selected = {int(iid): int(spin.value()) for iid,(spin,_line,_net_value) in spinboxes.items() if spin.value() > 0}
                if not selected:
                    QMessageBox.information(dialog, "لم تحدد كمية", "حدد كمية واحدة على الأقل للإرجاع.")
                    return
                method = refund_method.currentText()
                try:
                    with get_connection() as con:
                        result = process_return(con,user=self.current_user,invoice_id=invoice_id,
                            item_quantities=selected,refund_method=method)
                    QMessageBox.information(dialog, "تم المرتجع",
                        f"تم تسجيل المرتجع بنجاح.\nرقم المرتجع: {result['return_number']}\n"
                        f"المبلغ: {money(result['refund_total'])}\n"
                        + ("تم خصم المبلغ من الخزنة النقدية للوردية الحالية." if method == "نقدي" else "تمت إضافة المبلغ إلى رصيد العميل."))
                    dialog.accept()
                    self.load_invoices(); self.load_products(); self.refresh_cashier_products()
                    self.refresh_shifts_page(); self.load_customers()
                except Exception as exc:
                    self.logger.exception("Sales return failed")
                    QMessageBox.critical(dialog, "تعذر تسجيل المرتجع", f"لم يتم اعتماد المرتجع.\n\nالتفاصيل: {exc}")

            save_btn.clicked.connect(save_return)
            cancel_btn.clicked.connect(dialog.reject)
            dialog.exec()
        except Exception as e:
            QMessageBox.critical(self, "خطأ", f"تعذر فتح شاشة المرتجعات:\n{e}")

    def show_sales_returns_history(self):
        if not self.require_admin(): return
        try:
            with get_connection() as con:
                rows = con.execute("""
                    SELECT sr.id,sr.return_number,COALESCE(i.daily_number,i.invoice_number),
                           sr.created_at,sr.total_refund,sr.refund_method,COALESCE(sr.shift_id,0),
                           (SELECT COALESCE(SUM(quantity),0) FROM sales_return_items WHERE return_id=sr.id)
                    FROM sales_returns sr JOIN invoices i ON i.id=sr.invoice_id
                    ORDER BY sr.id DESC
                """).fetchall()
            dialog = QDialog(self)
            dialog.setWindowTitle("سجل مرتجعات البيع")
            dialog.resize(900, 500)
            dialog.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
            layout = QVBoxLayout(dialog)
            table = QTableWidget(0, 8)
            table.setHorizontalHeaderLabels(["ID", "رقم المرتجع", "الفاتورة الأصلية", "التاريخ", "المبلغ المسترد", "عدد الأصناف", "طريقة الرد", "الوردية"])
            table.setColumnHidden(0, True)
            table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
            table.verticalHeader().setVisible(False)
            table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
            table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
            table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
            table.setRowCount(len(rows))
            for r, item in enumerate(rows):
                method = {"cash":"نقدي", "customer_credit":"رصيد العميل", "unknown":"غير مصنف"}.get(item[5], item[5])
                values = [item[0],item[1],item[2],item[3],money(item[4]),item[7],method,item[6] or "—"]
                for c, value in enumerate(values):
                    cell = QTableWidgetItem(str(value))
                    cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    table.setItem(r, c, cell)
                if item[5] == "unknown":
                    for c in range(1,8):
                        table.item(r,c).setForeground(Qt.GlobalColor.darkYellow)
            layout.addWidget(table)
            note = QLabel("المرتجعات القديمة غير المصنفة لا تؤثر على حساب الخزنة النقدي حتى يراجع المدير السجلات ويحدد طريقة الرد الفعلية.")
            note.setWordWrap(True); note.setObjectName("Muted"); layout.addWidget(note)
            buttons = QHBoxLayout()
            classify_btn = QPushButton("تصنيف مرتجع تاريخي محدد")
            classify_btn.setObjectName("Primary")
            classify_btn.setVisible(is_admin(self.current_user))
            classify_btn.clicked.connect(lambda: self.classify_legacy_return_dialog(table, dialog))
            close_btn = QPushButton("إغلاق")
            close_btn.clicked.connect(dialog.accept)
            buttons.addWidget(classify_btn); buttons.addStretch(); buttons.addWidget(close_btn)
            layout.addLayout(buttons)
            dialog.exec()
        except Exception as e:
            QMessageBox.critical(self, "خطأ", f"تعذر تحميل سجل المرتجعات:\n{e}")

    def classify_legacy_return_dialog(self, table, parent_dialog=None):
        if not self.require_admin():
            return
        row_index = table.currentRow()
        if row_index < 0 or table.item(row_index,0) is None:
            QMessageBox.information(self, "اختيار مرتجع", "اختر المرتجع التاريخي المطلوب تصنيفه أولًا.")
            return
        return_id = int(table.item(row_index,0).text())
        try:
            with get_connection() as con:
                item = con.execute("""
                    SELECT sr.return_number,sr.total_refund,sr.refund_method,sr.shift_id,
                           i.customer_id,i.shift_id AS original_shift
                    FROM sales_returns sr JOIN invoices i ON i.id=sr.invoice_id WHERE sr.id=?
                """, (return_id,)).fetchone()
                shifts = con.execute("""
                    SELECT s.id,s.opened_at,COALESCE(s.closed_at,'مفتوحة'),COALESCE(u.display_name,'')
                    FROM cash_shifts s LEFT JOIN users u ON u.id=s.user_id ORDER BY s.id DESC
                """).fetchall()
            if not item or item[2] != "unknown":
                QMessageBox.information(self, "لا يحتاج تصنيفًا", "هذا المرتجع مصنّف بالفعل أو لم يعد موجودًا.")
                return
            form_dialog = QDialog(parent_dialog or self)
            form_dialog.setWindowTitle("تصنيف مرتجع تاريخي")
            form_dialog.setMinimumWidth(480)
            form_dialog.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
            form = QFormLayout(form_dialog)
            details = QLabel(f"المرتجع: {item[0]}\nالمبلغ: {money(item[1])}\nيجب تحديد طريقة الرد اعتمادًا على السجلات الفعلية، وليس التخمين.")
            details.setWordWrap(True)
            method = QComboBox(); method.addItems(["نقدي"] + (["رصيد العميل"] if item[4] else []))
            shift_combo = QComboBox()
            shift_combo.addItem("— اختر الوردية من السجلات الفعلية —", None)
            for shift_id, opened_at, closed_at, user_name in shifts:
                shift_combo.addItem(f"#{shift_id} — {user_name} — {opened_at} إلى {closed_at}", int(shift_id))
            shift_combo.setCurrentIndex(0)
            shift_combo.setEnabled(bool(shifts))
            method.currentTextChanged.connect(lambda value: shift_combo.setEnabled(value == "نقدي" and bool(shifts)))
            form.addRow(details)
            form.addRow("طريقة الرد الفعلية:", method)
            form.addRow("الوردية التي خرج منها النقد:", shift_combo)
            buttons = QHBoxLayout(); save = QPushButton("تأكيد التصنيف"); save.setObjectName("Primary"); cancel = QPushButton("إلغاء")
            buttons.addStretch(); buttons.addWidget(cancel); buttons.addWidget(save); form.addRow(buttons)
            cancel.clicked.connect(form_dialog.reject)
            def save_classification():
                chosen_method = method.currentText()
                chosen_shift = shift_combo.currentData() if chosen_method == "نقدي" else None
                if chosen_method == "نقدي" and chosen_shift is None:
                    QMessageBox.warning(form_dialog, "الوردية مطلوبة", "اختر الوردية التي تم رد المبلغ منها.")
                    return
                answer = QMessageBox.question(form_dialog, "تأكيد التصنيف التاريخي",
                    "أؤكد أن طريقة الرد والوردية المختارتين مطابقتان للسجلات الفعلية؟\nسيؤثر التصنيف النقدي على تسوية الوردية التاريخية.",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.No)
                if answer != QMessageBox.StandardButton.Yes:
                    return
                try:
                    with get_connection() as con:
                        result = classify_legacy_return(con,admin_user=self.current_user,
                            return_id=return_id,refund_method=chosen_method,shift_id=chosen_shift)
                    reconciliation = (f"\nالرصيد المتوقع للوردية: {money(result['old_expected_cash'])} ← {money(result['new_expected_cash'])}"
                        if result.get("new_expected_cash") is not None else "")
                    QMessageBox.information(form_dialog, "تم التصنيف",
                        f"تم تصنيف المرتجع {result['return_id']} كـ {chosen_method}.\nتم تحديث دفتر العميل/تسوية الوردية والسجل الرقابي.{reconciliation}")
                    form_dialog.accept()
                    if parent_dialog:
                        parent_dialog.accept()
                    self.refresh_shifts_page(); self.load_customers(); self.show_sales_returns_history()
                except Exception as exc:
                    self.logger.exception("Legacy return classification failed")
                    QMessageBox.critical(form_dialog, "تعذر التصنيف", str(exc))
            save.clicked.connect(save_classification)
            form_dialog.exec()
        except Exception as exc:
            self.logger.exception("Unable to open legacy return classifier")
            QMessageBox.critical(self, "خطأ", f"تعذر تصنيف المرتجع التاريخي:\n{exc}")

    def create_reports_page(self):
        page = QWidget()
        outer_layout = QVBoxLayout(page); outer_layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        content = QWidget(); layout = QVBoxLayout(content); layout.setContentsMargins(14, 14, 14, 18); layout.setSpacing(12)

        header = QHBoxLayout()
        title = QLabel("لوحة تحكم التقارير والأرباح"); title.setObjectName("PageTitle")
        header.addWidget(title); header.addStretch()
        refresh_top = QPushButton("⟳ تحديث البيانات"); refresh_top.setObjectName("Primary"); refresh_top.clicked.connect(self.load_reports); header.addWidget(refresh_top)
        layout.addLayout(header)
        intro = QLabel("نظرة سريعة على أداء المحل. اضغط مرتين على فاتورة لفتح تفاصيلها، أو على شهر في الملخص لعرض تفاصيله.")
        intro.setObjectName("Muted"); intro.setWordWrap(True); layout.addWidget(intro)

        filters = QFrame(); filters.setObjectName("Card")
        filter_layout = QHBoxLayout(filters); filter_layout.setContentsMargins(12, 10, 12, 10)
        filter_layout.addWidget(QLabel("الفترة:"))
        self.report_period = QComboBox(); self.report_period.addItems(["هذا الشهر", "اليوم", "هذا الربع", "هذه السنة", "فترة مخصصة"])
        self.report_period.setMinimumWidth(130); filter_layout.addWidget(self.report_period)
        filter_layout.addWidget(QLabel("من:"))
        business_today = QDate.fromString(business_date_for(datetime.now()), "yyyy-MM-dd")
        self.report_from = QDateEdit(); self.report_from.setCalendarPopup(True); self.report_from.setDisplayFormat("yyyy-MM-dd"); self.report_from.setDate(business_today); filter_layout.addWidget(self.report_from)
        filter_layout.addWidget(QLabel("إلى:"))
        self.report_to = QDateEdit(); self.report_to.setCalendarPopup(True); self.report_to.setDisplayFormat("yyyy-MM-dd"); self.report_to.setDate(business_today); filter_layout.addWidget(self.report_to)
        refresh = QPushButton("عرض التقرير"); refresh.setObjectName("Primary"); refresh.clicked.connect(self.load_reports); filter_layout.addWidget(refresh)
        all_time = QPushButton("كل الفترة"); all_time.clicked.connect(self.report_all_time); filter_layout.addWidget(all_time)
        layout.addWidget(filters)

        self.report_summary = QLabel("اختر الفترة ثم اضغط عرض التقرير."); self.report_summary.setWordWrap(True)
        self.report_summary.setStyleSheet(T.qss("background:@info_bg@;border:1px solid @info_border@;border-radius:10px;padding:12px;font-size:13px;color:@info@;"))
        layout.addWidget(self.report_summary)

        metrics_frame = QFrame(); metrics_frame.setObjectName("Card")
        metrics_layout = QGridLayout(metrics_frame); metrics_layout.setContentsMargins(12,12,12,12); metrics_layout.setSpacing(10)
        def report_metric(title_text, color, symbol):
            card = QFrame(); card.setStyleSheet(T.qss("QFrame{background:@surface@;border:1px solid @border_soft@;border-radius:12px;} QFrame:hover{border:1px solid @border_strong@;}"))
            box = QVBoxLayout(card); box.setContentsMargins(13,11,13,11); box.setSpacing(6)
            top = QHBoxLayout(); label = QLabel(title_text); label.setObjectName("Muted"); icon = QLabel(symbol); icon.setStyleSheet(f"font-size:18px;font-weight:bold;color:{color};"); top.addWidget(label); top.addStretch(); top.addWidget(icon); box.addLayout(top)
            value = QLabel("—"); value.setWordWrap(True); value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse); value.setStyleSheet(f"font-size:19px;font-weight:bold;color:{color};")
            box.addWidget(value)
            return card, value
        metric_defs = [
            ("صافي المبيعات", T.SUCCESS, "↗"), ("صافي الربح التقديري", T.INFO, "∑"),
            ("عدد الفواتير", T.TEXT, "▤"), ("متوسط الفاتورة", T.PRIMARY_STRONG, "◷"),
            ("الخصومات", T.WARNING, "%"), ("المرتجعات", T.ERROR, "↶"),
            ("تكلفة البضاعة", T.TEXT_SECONDARY, "□"), ("مصروفات الخزنة", T.PRIMARY_DEEP, "−")]
        self.report_metric_values = {}
        for idx, (metric_title, metric_color, symbol) in enumerate(metric_defs):
            card, value = report_metric(metric_title, metric_color, symbol)
            metrics_layout.addWidget(card, idx // 4, idx % 4); self.report_metric_values[metric_title] = value
        layout.addWidget(metrics_frame)

        charts_row = QHBoxLayout(); charts_row.setSpacing(12)
        sales_chart_frame = QFrame(); sales_chart_frame.setObjectName("Card"); sales_chart_layout = QVBoxLayout(sales_chart_frame); sales_chart_layout.setContentsMargins(12,10,12,8)
        self.report_sales_chart = ReportBarChart("حركة المبيعات خلال الفترة")
        sales_chart_layout.addWidget(self.report_sales_chart)
        product_chart_frame = QFrame(); product_chart_frame.setObjectName("Card"); product_chart_layout = QVBoxLayout(product_chart_frame); product_chart_layout.setContentsMargins(12,10,12,8)
        self.report_products_chart = ReportBarChart("أكثر المنتجات مبيعًا")
        product_chart_layout.addWidget(self.report_products_chart)
        charts_row.addWidget(sales_chart_frame, 3); charts_row.addWidget(product_chart_frame, 2); layout.addLayout(charts_row)

        products_title = QLabel("المنتجات الأكثر مبيعًا — تفاصيل"); products_title.setStyleSheet("font-size:18px;font-weight:bold;margin-top:4px;"); layout.addWidget(products_title)
        self.report_products_table = QTableWidget(0, 3); self.report_products_table.setHorizontalHeaderLabels(["اسم المنتج", "الكمية المباعة", "قيمة البيع"])
        self.report_products_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch); self.report_products_table.verticalHeader().setVisible(False)
        self.report_products_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers); self.report_products_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.report_products_table.setMaximumHeight(230); layout.addWidget(self.report_products_table)

        monthly_title = QLabel("ملخص الأداء بالشهور"); monthly_title.setStyleSheet("font-size:18px;font-weight:bold;margin-top:4px;"); layout.addWidget(monthly_title)
        self.report_monthly_table = QTableWidget(0, 7); self.report_monthly_table.setHorizontalHeaderLabels(["الشهر", "عدد الفواتير", "المبيعات", "الخصومات", "المرتجعات", "المصروفات", "الربح التقديري"])
        self.report_monthly_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch); self.report_monthly_table.verticalHeader().setVisible(False)
        self.report_monthly_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers); self.report_monthly_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.report_monthly_table.setMaximumHeight(260); self.report_monthly_table.cellDoubleClicked.connect(self.open_report_month)
        layout.addWidget(self.report_monthly_table)

        invoices_title = QLabel("تفاصيل الفواتير (اضغط مرتين لفتح التفاصيل)"); invoices_title.setStyleSheet("font-size:18px;font-weight:bold;margin-top:4px;"); layout.addWidget(invoices_title)
        self.report_table = QTableWidget(0, 6); self.report_table.setHorizontalHeaderLabels(["رقم الفاتورة", "التاريخ", "عدد الأصناف", "قبل الخصم", "الخصم", "الصافي"])
        self.report_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch); self.report_table.verticalHeader().setVisible(False)
        self.report_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers); self.report_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.report_table.cellDoubleClicked.connect(self.open_report_invoice); layout.addWidget(self.report_table)
        note = QLabel("تنبيه محاسبي: الربح تقديري، ويعتمد على تكلفة الشراء المحفوظة في سطور البيع، مطروحًا منها تكلفة المرتجعات ومصروفات الخزنة المسجلة. المصروفات غير المسجلة والفواتير القديمة التي لا تحتوي على تكلفة قد تؤثر على الدقة.")
        note.setWordWrap(True); note.setObjectName("Muted"); layout.addWidget(note)
        self.report_period.currentTextChanged.connect(self.apply_report_period)
        self.apply_report_period(self.report_period.currentText(), refresh=False)
        scroll.setWidget(content); outer_layout.addWidget(scroll)
        return page

    def apply_report_period(self, period, refresh=True):
        today = QDate.fromString(business_date_for(datetime.now()), "yyyy-MM-dd")
        if period == "فترة مخصصة":
            if refresh: self.load_reports()
            return
        if period == "اليوم":
            start = today; end = today
        elif period == "هذا الشهر":
            start = QDate(today.year(), today.month(), 1); end = today
        elif period == "هذا الربع":
            first_month = ((today.month() - 1) // 3) * 3 + 1
            start = QDate(today.year(), first_month, 1); end = today
        else:
            start = QDate(today.year(), 1, 1); end = today
        self.report_from.blockSignals(True); self.report_to.blockSignals(True)
        self.report_from.setDate(start); self.report_to.setDate(end)
        self.report_from.blockSignals(False); self.report_to.blockSignals(False)
        if refresh: self.load_reports()

    def report_all_time(self):
        self.report_period.blockSignals(True); self.report_period.setCurrentText("فترة مخصصة"); self.report_period.blockSignals(False)
        self.report_from.setDate(QDate(2000, 1, 1))
        self.report_to.setDate(QDate.fromString(business_date_for(datetime.now()), "yyyy-MM-dd"))
        self.load_reports()

    def load_reports(self):
        if not self.require_admin(): return
        if not hasattr(self, "report_table"):
            return
        date_from = self.report_from.date().toString("yyyy-MM-dd")
        date_to = self.report_to.date().toString("yyyy-MM-dd")
        if date_from > date_to:
            QMessageBox.warning(self, "فترة غير صحيحة", "تاريخ البداية يجب أن يكون قبل تاريخ النهاية.")
            return
        try:
            with get_connection() as con:
                snapshot = get_report_snapshot(con,date_from,date_to)
            count=snapshot["count"]; subtotal=snapshot["subtotal"]; discounts=snapshot["discounts"]
            sales_total=snapshot["sales_total"]; returns_total=snapshot["returns_total"]
            net_sales=snapshot["net_sales"]; net_cost=snapshot["net_cost"]
            expenses=snapshot["expenses"]; estimated_profit=snapshot["estimated_profit"]
            unknown_cost_items=snapshot["unknown_cost_units"]
            unknown_rev=snapshot["unknown_cost_revenue"]; audited_profit=snapshot["audited_net_profit"]
            unknown_products=snapshot["unknown_cost_products"]
            unknown_refunds=snapshot["unclassified_return_total"]
            refund_warning = (f"\nتحذير تسوية: توجد مرتجعات بقيمة {money(unknown_refunds)} لا نعرف هل رُدت نقدًا أم كرصيد عميل؛ لا يمكن حسم أثرها على الخزنة التاريخية دون مراجعة السجلات.") if unknown_refunds else ""
            has_unknown = bool(unknown_cost_items or unknown_rev)
            # Never present a profit that silently treats unknown cost as zero: with unknown-cost sales the
            # headline figure is the AUDITED profit (known-cost sales only) and the gap is spelled out.
            headline_profit = audited_profit if has_unknown else estimated_profit
            profit_label = "صافي الربح (للأصناف معلومة التكلفة فقط)" if has_unknown else "صافي الربح التقديري"
            if has_unknown:
                cost_warning=(f"⚠ الربح غير مكتمل: {unknown_cost_items} وحدة مباعة بتكلفة غير معروفة (مبيعات بقيمة {money(unknown_rev)}) "
                              f"مستبعدة من الربح ولم تُحتسب بتكلفة صفر. لا يمكن اعتبار الربح التاريخي دقيقًا.")
            else:
                cost_warning="الربح تقديري: التكلفة بمتوسط التكلفة المتحرك وقت البيع."
            if unknown_products:
                cost_warning+=(f"\n⚠ يوجد {len(unknown_products)} صنف بمخزون موجود وتكلفة غير معروفة: "
                               + "، ".join(p["name"] for p in unknown_products[:5]) + (" ..." if len(unknown_products)>5 else "")
                               + ". حدّد تكلفتها عند أول استلام شراء حتى لا تُحسب مجانية.")
            self.report_summary.setText(
                f"الفترة التجارية: {date_from} إلى {date_to}\nعدد الفواتير: {count} | إجمالي البيع قبل الخصم: {money(subtotal)} | الخصومات: {money(discounts)}\n"
                f"إجمالي المبيعات بعد الخصم: {money(sales_total)} | المرتجعات: {money(returns_total)} | صافي المبيعات: {money(net_sales)}\n"
                f"تكلفة البضاعة الصافية المسجلة: {money(net_cost)} | مصروفات الخزنة: {money(expenses)} | {profit_label}: {money(headline_profit)}\n"
                + cost_warning + refund_warning
            )
            avg_invoice=(sales_total/count) if count else 0
            metric_values={"صافي المبيعات":money(net_sales),"صافي الربح التقديري":money(headline_profit)+(" *" if has_unknown else ""),
                "عدد الفواتير":f"{count:,} فاتورة","متوسط الفاتورة":money(avg_invoice),
                "الخصومات":money(discounts),"المرتجعات":money(returns_total),
                "تكلفة البضاعة":money(net_cost),"مصروفات الخزنة":money(expenses)}
            for title,value in metric_values.items():
                self.report_metric_values[title].setText(value)

            trend_rows=snapshot["trend_rows"]; trend_returns=snapshot["trend_returns"]
            trend_values=[]
            for label,value in trend_rows:
                display_label=label[5:] if len(label)==10 else label
                trend_values.append((display_label,int(value)-int(trend_returns.get(label,0))))
            self.report_sales_chart.set_data(trend_values,T.PRIMARY_STRONG)

            top_products=snapshot["top_products"]
            self.report_products_chart.set_data([(name,qty) for name,qty,_amount in reversed(top_products)],T.PRIMARY_STRONG,value_divisor=1)
            self.report_products_table.setRowCount(len(top_products))
            for r,(name,qty,amount) in enumerate(top_products):
                for c,value in enumerate([name,f"{qty:g}",money(amount)]):
                    cell=QTableWidgetItem(str(value)); cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter); self.report_products_table.setItem(r,c,cell)

            rows=snapshot["invoice_rows"]
            self.report_table.setRowCount(len(rows))
            for r,row in enumerate(rows):
                vals=[row[1],row[2],row[6],money(row[3]),money(row[4]),money(row[5])]
                for c,value in enumerate(vals):
                    cell=QTableWidgetItem(str(value)); cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter); self.report_table.setItem(r,c,cell)
                self.report_table.item(r,0).setData(Qt.ItemDataRole.UserRole,row[0])

            monthly_rows=snapshot["monthly_rows"]
            self.report_monthly_table.setRowCount(len(monthly_rows))
            for r,row in enumerate(monthly_rows):
                month_key,month_count,month_sales,month_discount=row
                month_return=snapshot["monthly_returns"].get(month_key,0)
                month_cost=snapshot["monthly_costs"].get(month_key,0)-snapshot["monthly_return_costs"].get(month_key,0)
                month_expense=snapshot["monthly_expenses"].get(month_key,0)
                month_profit=month_sales-month_return-month_cost-month_expense
                vals=[month_key,month_count,money(month_sales),money(month_discount),money(month_return),money(month_expense),money(month_profit)]
                for c,value in enumerate(vals):
                    cell=QTableWidgetItem(str(value)); cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter); self.report_monthly_table.setItem(r,c,cell)
        except Exception as exc:
            self.logger.exception("Report refresh failed")
            QMessageBox.critical(self, "خطأ في التقرير", f"تعذر تحميل التقرير:\n{exc}")

    def open_report_month(self, row, _column):
        item = self.report_monthly_table.item(row, 0)
        if not item:
            return
        key = item.text().strip()
        try:
            year, month = [int(part) for part in key.split("-")[:2]]
            start = QDate(year, month, 1)
            end = QDate(year, month, start.daysInMonth())
            self.report_period.blockSignals(True); self.report_period.setCurrentText("فترة مخصصة"); self.report_period.blockSignals(False)
            self.report_from.setDate(start); self.report_to.setDate(end)
            self.load_reports()
        except (ValueError, TypeError):
            return

    def open_report_invoice(self, row, _column):
        item = self.report_table.item(row, 0)
        if item:
            invoice_id = item.data(Qt.ItemDataRole.UserRole)
            self.show_invoice_details(int(invoice_id))

    def create_suppliers_page(self):
        page = QWidget(); layout = QVBoxLayout(page); layout.setSpacing(14)
        header = QHBoxLayout(); title = QLabel("إدارة الموردين"); title.setObjectName("PageTitle"); header.addWidget(title); header.addStretch()
        add_btn = QPushButton("＋ إضافة مورد"); add_btn.setObjectName("Primary"); add_btn.clicked.connect(lambda: self.open_supplier_dialog()); header.addWidget(add_btn)
        edit_btn = QPushButton("تعديل المحدد"); edit_btn.clicked.connect(self.edit_selected_supplier); header.addWidget(edit_btn)
        delete_btn = QPushButton("حذف المحدد"); delete_btn.setObjectName("Danger"); delete_btn.clicked.connect(self.delete_selected_supplier); header.addWidget(delete_btn)
        refresh_btn = QPushButton("تحديث"); refresh_btn.clicked.connect(self.load_suppliers); header.addWidget(refresh_btn); layout.addLayout(header)
        self.supplier_search = QLineEdit(); self.supplier_search.setPlaceholderText("ابحث باسم المورد أو رقم الهاتف..."); self.supplier_search.textChanged.connect(self.load_suppliers); layout.addWidget(self.supplier_search)
        self.supplier_table = QTableWidget(0, 4); self.supplier_table.setHorizontalHeaderLabels(["اسم المورد", "رقم الهاتف", "العنوان", "ملاحظات"])
        self.supplier_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers); self.supplier_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows); self.supplier_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection); self.supplier_table.setAlternatingRowColors(True); self.supplier_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch); self.supplier_table.verticalHeader().setVisible(False); self.supplier_table.cellDoubleClicked.connect(lambda _r, _c: self.edit_selected_supplier()); layout.addWidget(self.supplier_table, 1)
        self.supplier_status = QLabel(""); self.supplier_status.setObjectName("Muted"); layout.addWidget(self.supplier_status)
        return page

    def load_suppliers(self):
        if not self.require_admin(): return
        if not hasattr(self, "supplier_table"): return
        search = self.supplier_search.text().strip() if hasattr(self, "supplier_search") else ""
        try:
            with get_connection() as con:
                rows = con.execute("SELECT id,name,phone,address,notes FROM suppliers WHERE name LIKE ? OR phone LIKE ? ORDER BY name", (f"%{search}%", f"%{search}%")).fetchall()
            self.supplier_table.setRowCount(len(rows))
            for r, row in enumerate(rows):
                for c, value in enumerate(row[1:]):
                    cell = QTableWidgetItem(str(value or "")); cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter); self.supplier_table.setItem(r, c, cell)
                self.supplier_table.item(r, 0).setData(Qt.ItemDataRole.UserRole, row[0])
            self.supplier_status.setText(f"عدد الموردين: {len(rows)} — اضغط مرتين على المورد لتعديله.")
        except Exception as e:
            QMessageBox.critical(self, "خطأ", f"تعذر تحميل الموردين:\n{e}")

    def open_supplier_dialog(self, supplier_id=None):
        if not self.require_admin(): return
        existing = None
        if supplier_id is not None:
            with get_connection() as con:
                existing = con.execute("SELECT name,phone,address,notes FROM suppliers WHERE id=?", (supplier_id,)).fetchone()
            if not existing: return
        dialog = QDialog(self); dialog.setWindowTitle("تعديل بيانات مورد" if existing else "إضافة مورد جديد"); dialog.resize(480, 320); dialog.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        layout = QVBoxLayout(dialog); form = QFormLayout()
        name = QLineEdit(existing[0] if existing else ""); phone = QLineEdit(existing[1] if existing else ""); address = QLineEdit(existing[2] if existing else ""); notes = QLineEdit(existing[3] if existing else "")
        name.setPlaceholderText("اسم المورد مطلوب"); phone.setPlaceholderText("رقم الهاتف"); address.setPlaceholderText("العنوان"); notes.setPlaceholderText("ملاحظات إضافية")
        form.addRow("اسم المورد:", name); form.addRow("رقم الهاتف:", phone); form.addRow("العنوان:", address); form.addRow("ملاحظات:", notes); layout.addLayout(form)
        buttons = QHBoxLayout(); cancel = QPushButton("إلغاء"); save = QPushButton("حفظ البيانات"); save.setObjectName("Primary"); buttons.addStretch(); buttons.addWidget(cancel); buttons.addWidget(save); layout.addLayout(buttons); cancel.clicked.connect(dialog.reject)
        def save_supplier():
            value = name.text().strip()
            if not value:
                QMessageBox.warning(dialog, "بيانات ناقصة", "اكتب اسم المورد أولًا."); return
            try:
                with get_connection() as con:
                    catalog_service.save_supplier(con, actor=self.current_user, name=value, phone=phone.text(), address=address.text(),
                                                  notes=notes.text(), supplier_id=supplier_id)
                dialog.accept(); self.load_suppliers(); self.load_purchases()
            except (ValueError, PermissionError) as e:
                QMessageBox.warning(dialog, "تعذر الحفظ", str(e))
            except Exception as e:
                QMessageBox.critical(dialog, "تعذر الحفظ", f"لم يتم حفظ بيانات المورد:\n{e}")
        save.clicked.connect(save_supplier); dialog.exec()

    def edit_selected_supplier(self):
        if not self.require_admin(): return
        row = self.supplier_table.currentRow() if hasattr(self, "supplier_table") else -1
        if row < 0 or not self.supplier_table.item(row, 0):
            QMessageBox.information(self, "اختيار مورد", "حدد موردًا من الجدول أولًا."); return
        self.open_supplier_dialog(int(self.supplier_table.item(row, 0).data(Qt.ItemDataRole.UserRole)))

    def delete_selected_supplier(self):
        if not self.require_admin(): return
        row = self.supplier_table.currentRow() if hasattr(self, "supplier_table") else -1
        if row < 0 or not self.supplier_table.item(row, 0):
            QMessageBox.information(self, "اختيار مورد", "حدد موردًا من الجدول أولًا."); return
        supplier_id = int(self.supplier_table.item(row, 0).data(Qt.ItemDataRole.UserRole))
        with get_connection() as con:
            used = con.execute("SELECT COUNT(*) FROM purchase_invoices WHERE supplier_id=?", (supplier_id,)).fetchone()[0]
        if used:
            QMessageBox.warning(self, "لا يمكن الحذف", "المورد مرتبط بفواتير شراء. احتفظ به حتى لا تفقد سجل الموردين."); return
        answer = QMessageBox.question(self, "تأكيد الحذف", "هل تريد حذف المورد المحدد؟", QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            try:
                with get_connection() as con: catalog_service.delete_supplier(con, actor=self.current_user, supplier_id=supplier_id)
                self.load_suppliers()
            except (ValueError, PermissionError) as e: QMessageBox.warning(self, "تعذر الحذف", str(e))
            except Exception as e: QMessageBox.critical(self, "خطأ", f"تعذر حذف المورد:\n{e}")

    def create_purchases_page(self):
        page = QWidget(); layout = QVBoxLayout(page); layout.setSpacing(14)
        header = QHBoxLayout(); title = QLabel("المشتريات وإضافة المخزون"); title.setObjectName("PageTitle"); header.addWidget(title); header.addStretch()
        add_btn = QPushButton("＋ تسجيل فاتورة شراء جديدة"); add_btn.setObjectName("Primary"); add_btn.clicked.connect(self.open_new_purchase_dialog); header.addWidget(add_btn)
        refresh_btn = QPushButton("تحديث"); refresh_btn.clicked.connect(self.load_purchases); header.addWidget(refresh_btn); layout.addLayout(header)
        info = QLabel("سجّل الأصناف والكميات وتكلفة الشراء. عند حفظ الفاتورة، تُضاف الكميات للمخزون تلقائيًا."); info.setObjectName("Muted"); info.setWordWrap(True); layout.addWidget(info)
        self.purchase_table = QTableWidget(0, 5); self.purchase_table.setHorizontalHeaderLabels(["رقم الفاتورة", "التاريخ", "المورد", "عدد الأصناف", "الإجمالي"])
        self.purchase_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers); self.purchase_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows); self.purchase_table.setAlternatingRowColors(True); self.purchase_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch); self.purchase_table.verticalHeader().setVisible(False); self.purchase_table.cellDoubleClicked.connect(self.show_purchase_details); layout.addWidget(self.purchase_table, 1)
        self.purchase_status = QLabel(""); self.purchase_status.setObjectName("Muted"); layout.addWidget(self.purchase_status)
        return page

    def load_purchases(self):
        if not self.require_admin(): return
        if not hasattr(self, "purchase_table"): return
        try:
            with get_connection() as con:
                rows = con.execute("""SELECT p.id,p.invoice_number,p.created_at,p.supplier_name,
                    (SELECT COUNT(*) FROM purchase_items i WHERE i.purchase_invoice_id=p.id),p.total
                    FROM purchase_invoices p ORDER BY p.id DESC""").fetchall()
            self.purchase_table.setRowCount(len(rows))
            for r, row in enumerate(rows):
                for c, value in enumerate(row[1:]):
                    shown = money(value) if c == 4 else str(value)
                    item = QTableWidgetItem(shown); item.setTextAlignment(Qt.AlignmentFlag.AlignCenter); self.purchase_table.setItem(r,c,item)
                self.purchase_table.item(r,0).setData(Qt.ItemDataRole.UserRole, row[0])
            self.purchase_status.setText(f"عدد فواتير الشراء: {len(rows)} — اضغط مرتين على الفاتورة لعرض تفاصيلها.")
        except Exception as e:
            QMessageBox.critical(self,"خطأ",f"تعذر تحميل المشتريات:\n{e}")

    def open_new_purchase_dialog(self):
        if not self.require_admin(): return
        with get_connection() as con:
            products = con.execute("SELECT id,code,COALESCE(barcode,''),name,COALESCE(size,''),COALESCE(color,''),COALESCE(category,'عام'),purchase_price,selling_price,COALESCE(min_stock,0) FROM products ORDER BY name").fetchall()
        dialog = QDialog(self); dialog.setWindowTitle("تسجيل فاتورة شراء"); dialog.resize(980,650); dialog.setLayoutDirection(Qt.LayoutDirection.RightToLeft)
        layout = QVBoxLayout(dialog); form = QFormLayout()
        supplier = QComboBox(); supplier.addItem("بدون مورد محدد", None)
        with get_connection() as con:
            supplier_rows = con.execute("SELECT id,name FROM suppliers ORDER BY name").fetchall()
        for supplier_row in supplier_rows: supplier.addItem(supplier_row[1], supplier_row[0])
        supplier.setToolTip("لإضافة مورد جديد، افتح صفحة الموردين أولًا")
        notes = QLineEdit(); notes.setPlaceholderText("ملاحظات (اختياري)")
        product_combo = QComboBox(); product_combo.setEditable(True); product_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert); product_combo.setMinimumWidth(400)
        product_combo.completer().setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive); product_combo.completer().setFilterMode(Qt.MatchFlag.MatchContains)
        new_products = {}
        def product_label(product):
            return f"{product['name']} | كود: {product['code']}" + (f" | مقاس: {product['size']}" if product['size'] else "") + (f" | لون: {product['color']}" if product['color'] else "")
        for product in products:
            product_combo.addItem(f"{product[3]} | كود: {product[1]}" + (f" | مقاس: {product[4]}" if product[4] else "") + (f" | لون: {product[5]}" if product[5] else ""), product[0])
        quantity = QSpinBox(); quantity.setRange(1,1000000); quantity.setValue(1)
        cost = FocusSelectDoubleSpinBox(); cost.setRange(0,10000000); cost.setDecimals(2); cost.setKeyboardTracking(True); cost.setSuffix(" جنيه")
        def update_cost(index):
            pid = product_combo.itemData(index)
            if pid is None: return
            if int(pid) < 0:
                product = new_products.get(int(pid))
                if product: cost.setValue(product['purchase'] / 100)
                return
            selected = next((x for x in products if int(x[0]) == int(pid)), None)
            if selected: cost.setValue(selected[7] / 100)
        product_combo.currentIndexChanged.connect(update_cost)
        form.addRow("المورد:",supplier); form.addRow("الصنف (اكتب للبحث):",product_combo); form.addRow("الكمية:",quantity); form.addRow("تكلفة الوحدة:",cost); form.addRow("ملاحظات:",notes); layout.addLayout(form)
        add_new_product = QPushButton("＋ إنشاء منتج جديد داخل المشتريات"); add_new_product.setObjectName("Primary"); layout.addWidget(add_new_product)
        add_line = QPushButton("إضافة الصنف للفاتورة"); add_line.setObjectName("Primary"); layout.addWidget(add_line)
        def create_purchase_product():
            data = self.product_form(parent=dialog, purchase_mode=True)
            if not data: return
            if any(str(x[1]).casefold() == data['code'].casefold() for x in products) or any(x['code'].casefold() == data['code'].casefold() for x in new_products.values()):
                QMessageBox.warning(dialog, "كود مكرر", "كود المنتج موجود بالفعل. استخدم كودًا مختلفًا."); return
            if data['barcode'] and (any(str(x[2] or '').casefold() == data['barcode'].casefold() for x in products) or any((x.get('barcode') or '').casefold() == data['barcode'].casefold() for x in new_products.values())):
                QMessageBox.warning(dialog, "باركود مكرر", "الباركود موجود بالفعل. استخدم باركودًا مختلفًا."); return
            temp_id = -(len(new_products) + 1)
            product = {'temp_id':temp_id,'code':data['code'],'barcode':data['barcode'],'name':data['name'],'size':data['size'],'color':data['color'],'category':data['category'],'purchase':data['purchase'],'selling':data['selling'],'minimum':data['minimum']}
            new_products[temp_id] = product
            product_combo.addItem(product_label(product), temp_id)
            product_combo.setCurrentIndex(product_combo.count()-1)
            cost.setValue(product['purchase']/100)
        add_new_product.clicked.connect(create_purchase_product)
        table = QTableWidget(0,5); table.setHorizontalHeaderLabels(["الصنف", "الكمية", "تكلفة الوحدة", "الإجمالي", "معرّف المنتج"]); table.setColumnHidden(4,True); table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch); table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers); table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows); layout.addWidget(table,1)
        total_label = QLabel("إجمالي فاتورة الشراء: 0.00 جنيه"); total_label.setStyleSheet(T.qss("font-size:18px;font-weight:bold;color:@text@;")); layout.addWidget(total_label)
        lines = []
        def refresh_lines():
            table.setRowCount(len(lines)); total = 0
            for r, line in enumerate(lines):
                total += line['quantity'] * line['cost']
                vals = [line['name'], str(line['quantity']), money(line['cost']), money(line['quantity']*line['cost']), str(line['product_id'])]
                for c, val in enumerate(vals):
                    cell=QTableWidgetItem(val); cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter); table.setItem(r,c,cell)
            total_label.setText(f"إجمالي فاتورة الشراء: {money(total)}")
        def add_purchase_line():
            pid_data = product_combo.currentData()
            if pid_data is None:
                QMessageBox.warning(dialog, "اختيار المنتج", "اختار منتجًا مسجلًا من القائمة أو أنشئ منتجًا جديدًا أولًا."); return
            pid_data = int(pid_data)
            if pid_data < 0:
                selected = new_products.get(pid_data)
                if not selected:
                    QMessageBox.warning(dialog, "منتج غير موجود", "اختار المنتج من القائمة مرة أخرى."); return
                line = {'product_id':None,'temp_id':pid_data,'code':selected['code'],'name':selected['name'] + (f" ({selected['size']})" if selected['size'] else '') + (f" - {selected['color']}" if selected['color'] else ''),'quantity':quantity.value(),'cost':to_piasters(cost.value())}
            else:
                selected = next((x for x in products if int(x[0]) == pid_data), None)
                if not selected:
                    QMessageBox.warning(dialog, "اختيار المنتج", "المنتج غير موجود في القائمة."); return
                line = {'product_id':pid_data,'temp_id':None,'code':selected[1],'name':selected[3] + (f" ({selected[4]})" if selected[4] else '') + (f" - {selected[5]}" if selected[5] else ''),'quantity':quantity.value(),'cost':to_piasters(cost.value())}
            existing = next((x for x in lines if x['product_id']==line['product_id'] and x.get('temp_id')==line.get('temp_id') and x['cost']==line['cost']),None)
            if existing: existing['quantity'] += line['quantity']
            else: lines.append(line)
            refresh_lines()
        add_line.clicked.connect(add_purchase_line)
        buttons = QHBoxLayout(); remove_btn=QPushButton("حذف الصنف المحدد"); save_btn=QPushButton("حفظ فاتورة الشراء"); save_btn.setObjectName("Primary"); cancel_btn=QPushButton("إلغاء")
        buttons.addWidget(remove_btn); buttons.addStretch(); buttons.addWidget(cancel_btn); buttons.addWidget(save_btn); layout.addLayout(buttons)
        def remove_line():
            row=table.currentRow()
            if 0 <= row < len(lines): lines.pop(row); refresh_lines()
        remove_btn.clicked.connect(remove_line); cancel_btn.clicked.connect(dialog.reject)
        def save_purchase():
            if not lines:
                QMessageBox.warning(dialog,"الفاتورة فارغة","أضف صنفًا واحدًا على الأقل قبل الحفظ."); return
            try:
                supplier_id = supplier.currentData()
                supplier_name = supplier.currentText() if supplier_id is not None else ""
                opening_costs = {}
                while True:
                    try:
                        with get_connection() as con:
                            saved = receive_purchase(con, user=self.current_user, supplier_id=supplier_id, supplier_name=supplier_name,
                                                     notes=notes.text().strip(), lines=lines, new_products=new_products,
                                                     opening_costs=opening_costs)
                        break
                    except UnknownCostError as unknown:
                        # The existing units have no known cost.  Never average them in as free: the
                        # administrator must state their unit cost explicitly (audited) or cancel.
                        for item in unknown.products:
                            value, accepted = QInputDialog.getDouble(dialog, "تكلفة المخزون الحالي غير معروفة",
                                f"الصنف «{item['name']}» لديه {item['stock']} وحدة بتكلفة غير معروفة.\n"
                                "استلام كمية جديدة سيحسب المتوسط وكأن هذه الوحدات مجانية، فيظهر الربح أعلى من الحقيقي.\n"
                                "أدخل تكلفة الوحدة الحقيقية للمخزون الحالي (يُسجَّل قرارك في سجل المراجعة)،\nأو اضغط إلغاء لإيقاف الحفظ:",
                                0.0, 0.0, 10_000_000.0, 2)
                            if not accepted or to_piasters(value) <= 0:
                                QMessageBox.information(dialog, "لم يتم الحفظ", "لم يتم حفظ فاتورة الشراء لأن تكلفة المخزون الحالي لم تُحدد."); return
                            opening_costs[int(item["id"])] = to_piasters(value)
                invoice_number = saved["invoice_number"]
                QMessageBox.information(dialog,"تم الحفظ",f"تم حفظ فاتورة الشراء {invoice_number} وتحديث المخزون بنجاح.")
                dialog.accept(); self.load_purchases(); self.load_products(); self.refresh_cashier_products()
            except Exception as e:
                QMessageBox.critical(dialog,"تعذر الحفظ",f"لم يتم حفظ الفاتورة. لم يتم اعتماد التغييرات:\n{e}")
        save_btn.clicked.connect(save_purchase)
        dialog.exec()

    def show_purchase_details(self, row, _column):
        if not self.require_admin(): return
        item=self.purchase_table.item(row,0)
        if not item: return
        purchase_id=item.data(Qt.ItemDataRole.UserRole)
        try:
            with get_connection() as con:
                header=con.execute("SELECT invoice_number,created_at,supplier_name,total,notes FROM purchase_invoices WHERE id=?",(purchase_id,)).fetchone()
                items=con.execute("SELECT product_name,quantity,unit_cost,line_total FROM purchase_items WHERE purchase_invoice_id=? ORDER BY id",(purchase_id,)).fetchall()
            if not header: return
            dialog=QDialog(self); dialog.setWindowTitle("تفاصيل فاتورة الشراء"); dialog.resize(700,450); dialog.setLayoutDirection(Qt.LayoutDirection.RightToLeft); layout=QVBoxLayout(dialog)
            summary=QLabel(f"رقم الفاتورة: {header[0]}\nالتاريخ: {header[1]}\nالمورد: {header[2] or '—'}\nالملاحظات: {header[4] or '—'}\nالإجمالي: {money(header[3])}"); summary.setWordWrap(True); layout.addWidget(summary)
            table=QTableWidget(0,4); table.setHorizontalHeaderLabels(["الصنف","الكمية","تكلفة الوحدة","الإجمالي"]); table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch); table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers); layout.addWidget(table)
            table.setRowCount(len(items))
            for r,line in enumerate(items):
                for c,val in enumerate([line[0],str(line[1]),money(line[2]),money(line[3])]):
                    cell=QTableWidgetItem(val); cell.setTextAlignment(Qt.AlignmentFlag.AlignCenter); table.setItem(r,c,cell)
            close=QPushButton("إغلاق"); close.clicked.connect(dialog.accept); layout.addWidget(close); dialog.exec()
        except Exception as e:
            QMessageBox.critical(self,"خطأ",f"تعذر عرض تفاصيل الفاتورة:\n{e}")


if __name__ == "__main__":
    app=QApplication(sys.argv)
    # Logging and the crash handler come FIRST so a startup failure (locked/corrupt DB, unwritable
    # data folder, failing migration) is logged and shown instead of silently closing the EXE.
    startup_logger = startup_guard.configure_logging(LOG_DIR)
    startup_guard.install_excepthook(startup_logger)
    try:
        window=SmartPOS()
    except Exception as startup_error:
        startup_guard.report_startup_failure(startup_logger, startup_error)
        sys.exit(1)
    if getattr(window, "_login_cancelled", False):
        sys.exit(0)
    window.show()
    sys.exit(app.exec())
