CREATE TABLE products (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                name TEXT NOT NULL,
                purchase_price INTEGER NOT NULL
                    CHECK (purchase_price >= 0),
                selling_price INTEGER NOT NULL
                    CHECK (selling_price >= 0),
                stock_quantity INTEGER NOT NULL DEFAULT 0
                    CHECK (stock_quantity >= 0),
                min_stock INTEGER NOT NULL DEFAULT 0
                    CHECK (min_stock >= 0)
            , barcode TEXT, size TEXT NOT NULL DEFAULT '', color TEXT NOT NULL DEFAULT '', category TEXT NOT NULL DEFAULT 'عام');

CREATE TABLE invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_number TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                subtotal INTEGER NOT NULL CHECK (subtotal >= 0),
                discount INTEGER NOT NULL DEFAULT 0 CHECK (discount >= 0),
                total INTEGER NOT NULL CHECK (total >= 0),
                payment_method TEXT NOT NULL DEFAULT 'نقدي',
                amount_paid INTEGER NOT NULL DEFAULT 0 CHECK (amount_paid >= 0),
                change_amount INTEGER NOT NULL DEFAULT 0 CHECK (change_amount >= 0)
            , shift_id INTEGER, user_id INTEGER, daily_number TEXT, business_date TEXT);

CREATE TABLE invoice_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                product_code TEXT NOT NULL,
                product_name TEXT NOT NULL,
                product_size TEXT NOT NULL DEFAULT '',
                product_color TEXT NOT NULL DEFAULT '',
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                unit_price INTEGER NOT NULL CHECK (unit_price >= 0),
                line_total INTEGER NOT NULL CHECK (line_total >= 0), unit_cost INTEGER NOT NULL DEFAULT -1,
                FOREIGN KEY(invoice_id) REFERENCES invoices(id) ON DELETE CASCADE,
                FOREIGN KEY(product_id) REFERENCES products(id)
            );

CREATE TABLE purchase_invoices (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                invoice_number TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                supplier_name TEXT NOT NULL DEFAULT '',
                total INTEGER NOT NULL CHECK (total >= 0),
                notes TEXT NOT NULL DEFAULT ''
            , supplier_id INTEGER REFERENCES suppliers(id));

CREATE TABLE purchase_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                purchase_invoice_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                product_code TEXT NOT NULL,
                product_name TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                unit_cost INTEGER NOT NULL CHECK (unit_cost >= 0),
                line_total INTEGER NOT NULL CHECK (line_total >= 0),
                FOREIGN KEY(purchase_invoice_id) REFERENCES purchase_invoices(id) ON DELETE CASCADE,
                FOREIGN KEY(product_id) REFERENCES products(id)
            );

CREATE TABLE sales_returns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                return_number TEXT NOT NULL UNIQUE,
                invoice_id INTEGER NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                total_refund INTEGER NOT NULL CHECK (total_refund >= 0),
                notes TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(invoice_id) REFERENCES invoices(id)
            );

CREATE TABLE sales_return_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                return_id INTEGER NOT NULL,
                invoice_item_id INTEGER NOT NULL,
                product_id INTEGER NOT NULL,
                product_name TEXT NOT NULL,
                quantity INTEGER NOT NULL CHECK (quantity > 0),
                unit_refund INTEGER NOT NULL CHECK (unit_refund >= 0),
                line_refund INTEGER NOT NULL CHECK (line_refund >= 0),
                FOREIGN KEY(return_id) REFERENCES sales_returns(id) ON DELETE CASCADE,
                FOREIGN KEY(invoice_item_id) REFERENCES invoice_items(id),
                FOREIGN KEY(product_id) REFERENCES products(id)
            );

CREATE TABLE suppliers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL COLLATE NOCASE UNIQUE,
                phone TEXT NOT NULL DEFAULT '',
                address TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );

CREATE TABLE users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL COLLATE NOCASE UNIQUE,
                display_name TEXT NOT NULL,
                password_hash TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('admin','cashier')),
                active INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime'))
            );

CREATE TABLE cash_shifts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                opened_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                opening_cash INTEGER NOT NULL DEFAULT 0 CHECK (opening_cash >= 0),
                closed_at TEXT,
                expected_cash INTEGER,
                actual_cash INTEGER,
                difference INTEGER,
                status TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','closed')),
                notes TEXT NOT NULL DEFAULT '',
                FOREIGN KEY(user_id) REFERENCES users(id)
            );

CREATE TABLE cash_movements (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                shift_id INTEGER NOT NULL,
                user_id INTEGER NOT NULL,
                movement_type TEXT NOT NULL CHECK (movement_type IN ('مصروف','سحب','إيداع')),
                amount INTEGER NOT NULL CHECK (amount > 0),
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT (datetime('now','localtime')),
                FOREIGN KEY(shift_id) REFERENCES cash_shifts(id),
                FOREIGN KEY(user_id) REFERENCES users(id)
            );

CREATE TABLE product_categories (name TEXT PRIMARY KEY COLLATE NOCASE);

CREATE UNIQUE INDEX idx_products_barcode_unique ON products(barcode) WHERE barcode IS NOT NULL AND barcode <> '';

CREATE INDEX idx_products_name ON products(name);

CREATE INDEX idx_invoice_items_invoice ON invoice_items(invoice_id);

CREATE INDEX idx_purchase_items_invoice ON purchase_items(purchase_invoice_id);

CREATE INDEX idx_sales_returns_invoice ON sales_returns(invoice_id);

CREATE INDEX idx_sales_return_items_return ON sales_return_items(return_id);

CREATE INDEX idx_cash_shifts_status ON cash_shifts(status);

CREATE INDEX idx_cash_movements_shift ON cash_movements(shift_id);

CREATE UNIQUE INDEX idx_invoices_business_daily_number ON invoices(business_date, daily_number) WHERE business_date IS NOT NULL AND daily_number IS NOT NULL;
