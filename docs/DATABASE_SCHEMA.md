# Database schema (summary)

Location: `%LOCALAPPDATA%\SmartPOS\smartpos.db` (Linux `~/.local/share/smartpos/`). WAL mode, `foreign_keys=ON`,
`busy_timeout=20s` on every connection (`database.get_connection`).

Core: `users`, `products`(+`product_groups`, `product_categories`), `invoices`, `invoice_items`,
`invoice_payments` (cash / credit / legacy_other), `customers`, `customer_ledger`, `sales_returns`,
`sales_return_items`, `suppliers`, `purchase_invoices`, `purchase_items`, `cash_shifts`, `cash_movements`,
`inventory_movements`, `audit_log`, `app_settings`, `schema_migrations`.

Money columns are INTEGER piasters. Append-only by trigger: `inventory_movements`, `customer_ledger`, `audit_log`,
`invoice_payments`, `sales_return_items`, `sales_returns` (delete). Other triggers enforce: invoice
total = subtotal − discount, credit ≤ total, credit invoice has a customer, payments ≤ total, invoice shift belongs to
its open user, one open shift per user, return lines belong to their invoice and cannot exceed sold quantity,
only `unknown` returns may be reclassified. Invoice `shift_id/user_id` are protected by triggers rather than physical
FKs (rebuilding the legacy table was judged riskier).
`PRAGMA integrity_check` and `foreign_key_check` are verified in tests and after every migration run.
