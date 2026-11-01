# SmartPOS — Database Recovery Report

Generated 2026-10-31 from the supplied `SmartPOS_Data_Recovery_Pack_2026-10-31.zip`. Every figure below was recomputed by reading the files read-only; nothing in this report is copied from the earlier manifest.

## 1. Verdict

**No database can be declared the production database from the evidence available.** The seven files contain only **four distinct byte-contents**, and at least two of those describe *conflicting* histories (same invoice id, different transaction). Choosing one requires facts that are not in the files (which machine the shop actually traded on, paper receipts, cash counts). This report therefore recommends a decision *procedure*, not a file.

## 2. Preserved originals

The uploaded ZIPs are kept unmodified. SHA-256 of the uploads:

| Upload | SHA-256 |
|---|---|
| `SmartPOS_Data_Recovery_Pack_2026-10-31.zip` | `eba4dff6b7e5c83fb2fcd65ecd70a0650ac4163dd01c935d1e787595db33f7aa` |
| `SmartPOS_Repaired_Source_1_0_0_2026-10-31.zip` | `c3a9bdc59fdc268b8f56b683a943f856762c1bda31f7ea2c9985c6433901f542` |
| `SmartPOS_Audit_Remediation_Status_2026-10-31.md` | `2f7df8e3f62a7d2860d254367f6aa9a6d2378ecb2cdfa2e9093e5fe7a13fe2c4` |

All seven `.db` files verified against the pack's own `SHA256SUMS.txt` (7/7 OK). Migration tests were run only on temporary copies; the extracted originals were made read-only and re-hashed after the run (unchanged).

## 3. Comparison (recomputed)

| File | Size | Products | Invoices | Lines | Shifts | Purchases | Returns | Latest invoice | Latest purchase | Latest shift | Σ invoice total (piasters) | Σ stock | Integrity | FK viol. | SHA-256 (first 12) |
|---|---:|---:|---:|---:|---:|---:|---:|---|---|---|---:|---:|---|---:|---|
| `build_backup/data/smartpos.db` | 135,168 | 5 | 16 | 32 | 6 | 2 | 2 | 2026-10-29 14:35:55 | 2026-10-29 14:33:43 | 2026-10-29 14:35:50 | 1,537,000 | 146 | ok | 0 | `d58804c6912b` |
| `build_backup/data/smartpos_backup.db` | 135,168 | 5 | 16 | 32 | 6 | 2 | 2 | 2026-10-29 14:35:55 | 2026-10-29 14:33:43 | 2026-10-29 14:35:50 | 1,537,000 | 146 | ok | 0 | `d58804c6912b` |
| `data/smartpos.db` | 135,168 | 5 | 17 | 36 | 7 | 2 | 2 | 2026-10-29 23:56:09 | 2026-10-29 14:33:43 | 2026-10-29 23:56:05 | 1,614,000 | 142 | ok | 0 | `b1b962913e67` |
| `data/smartpos_backup.db` | 135,168 | 5 | 16 | 32 | 6 | 2 | 2 | 2026-10-29 14:35:55 | 2026-10-29 14:33:43 | 2026-10-29 14:35:50 | 1,537,000 | 146 | ok | 0 | `d58804c6912b` |
| `data/smartpos_restore_pending.db` | 135,168 | 5 | 17 | 36 | 7 | 2 | 2 | 2026-10-29 23:56:09 | 2026-10-29 14:33:43 | 2026-10-29 23:56:05 | 1,614,000 | 142 | ok | 0 | `01a443679c40` |
| `dist/SmartPOS/data/smartpos.db` | 135,168 | 6 | 18 | 34 | 7 | 3 | 2 | 2026-10-30 07:34:47 | 2026-10-29 20:06:19 | 2026-10-29 20:04:28 | 1,582,000 | 163 | ok | 0 | `b4362c8aa349` |
| `dist/SmartPOS/data/smartpos_backup.db` | 135,168 | 5 | 16 | 32 | 6 | 2 | 2 | 2026-10-29 14:35:55 | 2026-10-29 14:33:43 | 2026-10-29 14:35:50 | 1,537,000 | 146 | ok | 0 | `d58804c6912b` |

All files: SQLite journal_mode `delete`, `user_version` 0, **no `schema_migrations` table** (pre-migration schema), same 12 tables.

### Identical-content groups

- **Group 1** `d58804c6912b`: `build_backup/data/smartpos.db`, `build_backup/data/smartpos_backup.db`, `data/smartpos_backup.db`, `dist/SmartPOS/data/smartpos_backup.db`
- **Group 2** `b1b962913e67`: `data/smartpos.db`
- **Group 3** `01a443679c40`: `data/smartpos_restore_pending.db`
- **Group 4** `b4362c8aa349`: `dist/SmartPOS/data/smartpos.db`

## 4. Why they conflict (row-level analysis)

| Pair | Result |
|---|---|
| build_backup vs data/smartpos.db | data/ = build_backup + invoice #17 (2026-10-29 23:56:09, 770.00). Strict superset. |
| data/smartpos.db vs data/smartpos_restore_pending.db | Same rows, **different bytes** (hash differs). Same logical content, not a different history. |
| data/smartpos.db vs dist/SmartPOS/data/smartpos.db | **Conflict.** Invoice #17 exists in both but is a *different sale* (`INV-20261029-235609…`, 770.00 vs `INV-20261029-200435…`, 300.00). dist additionally has invoice #18 (2026-10-30 07:34:47, 150.00), a 6th product (P006), a 3rd purchase invoice, and different stock levels. |

Consequence: neither is a superset of the other. A merge by id would silently overwrite or drop a real transaction. **Do not merge.**

## 5. Evidence still needed from the owner

1. Which computer/folder the shop actually used on 2026-10-29 evening and 2026-10-30 (the two branches diverge at invoice #17).
2. Paper/thermal receipts and the cash count for the shift containing invoices #17 and #18.
3. Whether product P006 (بلوزة) and its purchase exist in reality.

Decision procedure: pick the snapshot whose invoices #17/#18 match the paper records; if both are partly right, import ONE as the base and enter the missing sale(s) manually through the application, so every record gets a valid ledger/stock trail. Import with `import_legacy_database.py` (refuses to overwrite a non-empty database).

## 6. Migration results (copies of all 7 files, application code of this release)

Script: `scripts/verify_migrations_on_copies.py` — copies each file to a temp dir, runs `initialize_database()` **twice**, compares counts/totals, runs integrity and FK checks.

| File | Schema version | Row counts & totals preserved | Integrity | FK violations | Original untouched |
|---|---:|---|---|---:|---|
| `build_backup/data/smartpos.db` | 8 | yes | ok | 0 | yes |
| `build_backup/data/smartpos_backup.db` | 8 | yes | ok | 0 | yes |
| `data/smartpos.db` | 8 | yes | ok | 0 | yes |
| `data/smartpos_backup.db` | 8 | yes | ok | 0 | yes |
| `data/smartpos_restore_pending.db` | 8 | yes | ok | 0 | yes |
| `dist/SmartPOS/data/smartpos.db` | 8 | yes | ok | 0 | yes |
| `dist/SmartPOS/data/smartpos_backup.db` | 8 | yes | ok | 0 | yes |

After migration every snapshot shows: **2 legacy returns with refund method UNKNOWN (total 89,554 piasters = 895.54)** and **6 invoice lines with unknown historical cost** (`unit_cost = -1`). Stock equals the inventory ledger for every product (opening balance anchored at migration time; drift = 0).

## 7. Unresolved historical items (administrator must act)

- **2 returns of UNKNOWN refund method** (RET-20261029-014307-649465 = 450.00 and RET-20261029-143152-721053 = 445.54, both present in all seven files). Classify each in the application as cash (select the real shift) or customer credit, after checking receipts. The application will not guess; until classified they are excluded from expected cash and flagged on reports, and cash refunds on those invoices are blocked.
- **6 invoice lines with unknown cost.** Excluded from *audited* profit (reported separately as `unknown_cost_revenue`). Product P001 (`منتج تجريبي`) has cost 100.00 vs price 30.00 — a negative margin that looks like test data; verify before trusting any profit for it.
- All historical card/Visa invoices (if any appear in the chosen snapshot) stay readable as `legacy_other` payments; none exist in the current seven files (all 16–18 invoices are cash).

## 8. Rollback / recovery

Starting with this release, `initialize_database()` writes `migration_backups/pre_migration_v<old>_to_v8_<timestamp>.db` (SQLite backup API) before upgrading any existing database. To roll back: close the app, copy that file over `smartpos.db` (delete `smartpos.db-wal/-shm` first). The migration steps are additive and idempotent, so an interrupted upgrade is completed by simply starting the app again.

## 9. Provisional recommendation

Evidence-based ranking is impossible; the only safe default is: keep all seven files, start from a **fresh** install, import exactly one snapshot chosen by the procedure in §5, and re-enter the other branch's missing sales manually.
