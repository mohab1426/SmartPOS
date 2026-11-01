# Security

## Model
Two roles: `admin` and `cashier`. **Authorization is enforced in `services/*`, not in the UI.** Every privileged
service function re-reads the acting user from the database (`services/security.py: require_active_user /
require_active_admin`); a user dict passed by the UI is never trusted for its role. Verified by tests that call
the services directly (`tests/test_auth_permissions.py`).

| Operation | Who | Where enforced |
|---|---|---|
| Restore database | active admin + password re-entry | `services/backup.restore_database_file` |
| Create user / reset password / (de)activate user | active admin | `services/auth` |
| Change own password | any active user (needs current password) | `services/auth.change_own_password` |
| Stock adjustment, damage write-off, stocktake, purchase receiving | active admin | `services/inventory`, `services/purchasing` |
| Discount on a sale | active admin | `services/sales.create_sale` |
| Classify legacy returns | active admin | `services/sales.classify_legacy_return` |
| Change policy settings | active admin | `services/settings` |
| Product, category, supplier create/edit/delete | **UI-only guard (NOT enforced in services — open finding)** | `app.py` |
| Sale, collection, cash movement, close shift | any active user, **own open shift only** | `services/sales`, `customers`, `shifts` |
| Process return | admin; cashier only while setting `cashier_can_process_returns=1` (default 1) | `services/sales.process_return` |

## Passwords
PBKDF2-HMAC-SHA256, 240 000 iterations, 16-byte random per-user salt, stored as `salt$hexdigest`;
verification uses `hmac.compare_digest`. Policy: ≥ 8 characters, not a common password, not equal to the
username. There is **no default account**: the first run forces creation of the administrator
(`create_first_admin`, allowed only while the users table is empty). Login failures return one generic message
for unknown user / wrong password / inactive account and an unknown user costs the same hashing time.
5 consecutive failures lock the account for 5 minutes. Passwords are never written to the audit log.

## Restore
No restore happens automatically at startup. A leftover `smartpos_restore_pending.db` is moved to
`quarantine/` and never executed. See `BACKUP_AND_RESTORE.md`.

## Audit log
`audit_log` is append-only (triggers reject UPDATE/DELETE). Logged: sales, returns, classification of legacy
returns, purchases, cash movements, shift open/close, user/password/setting changes, account locks, restores.

## Known limitations
- The session user is held in memory by the GUI; there is no idle timeout/auto-lock.
- The database file itself is not encrypted. Anyone with filesystem access to `%LOCALAPPDATA%\SmartPOS` can
  read it or replace it while the app is closed; protect the Windows account and use disk encryption.
- Triggers are a second line of defence; a person with direct SQLite access can drop them.
- The cashier-returns policy default (allowed) preserves previous behaviour; the owner should decide.
