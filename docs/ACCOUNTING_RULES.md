# Accounting rules (as implemented)

**Money** is stored as integer piasters (1 EGP = 100). All arithmetic is exact. Where division is unavoidable
(invoice discount allocated over lines, partial-return refunds, weighted-average cost) the result is rounded
**half-up** at that single point; line values are allocated with the *largest-remainder* method so they always sum
exactly to the invoice total (no piaster created or lost). GUI input is converted with `Decimal`
(`services/money.to_piasters`), not `round(float*100)`.

**Payment methods for new sales:** cash (`نقدي`), customer credit (`آجل`), split (`نقدي + آجل`). Card/Visa/wallet
are rejected by `create_sale`; historical card invoices stay readable (`invoice_payments.tender='legacy_other'`).
Credit and split sales require a named customer. Change is given only on cash sales.

**Customer ledger** (`customer_ledger`, append-only): balance = Σdebit − Σcredit. Debit: credit sale. Credit:
collection, return credited to the account. `reporting.customer_ledger_discrepancies` re-derives debts and return
credits from invoices/returns and reports any mismatch. Customers are matched by (case-insensitive) name —
two different people with the same name share one account (known limitation).

**Returns:** against an original invoice line; cannot exceed sold quantity or earlier returns; refund is the
discount-adjusted line value (cumulative rounding, final unit absorbs the remainder). Cash refund requires the
refunding user's open shift, is limited to cash actually paid on that invoice, and reduces *that* shift. Credit
refund requires a customer account and does not touch cash. Disposition `restock` or `damaged`.
**Legacy returns** of unknown tender are never guessed: they stay `unknown`, are excluded from expected cash,
listed by `reporting.unclassified_returns`, and an admin classifies each (cash + real shift, or customer credit);
the shift's expected cash/variance is recomputed and the action audited.

**Expected cash** = opening + cash sales (cash tender allocations) + cash debt collections + cash-in
− cash-out/expenses − cash refunds. Credit sales and credit refunds are excluded. Expenses/withdrawals cannot exceed
the current expected drawer cash.

**Business day** starts at 04:00 local time (`accounting.business_date_for`, half-open `[04:00, 04:00)`). Every
report/dashboard figure filters on the stored `business_date` with inclusive date bounds. Cutoff is a constant,
**not yet configurable** and there is no timezone handling (the PC's local clock is the business clock).

**Profit:** *audited* profit uses only lines with a known cost; revenue on unknown-cost lines is reported separately
(`unknown_cost_revenue`). The older `estimated_profit` field still treats unknown cost as zero and must be read
together with `unknown_cost_units`. Expenses = cash movements of type `مصروف`.
