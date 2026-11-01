# SmartPOS Login and Authentication Audit

Scope: static reading of `services/auth.py`, `services/security.py`, and the login flow in `app.py`, plus the tests listed in SMARTPOS_TEST_RESULTS.md.

## Current behaviour (verified from source)
1. Startup: if `users` is empty -> first-run admin dialog (`create_first_admin`, refused if any user exists). Otherwise login dialog.
2. Identifier: **username only** (case-insensitive) + password. No employee-ID login, no password-only mode, no persistent/auto login. Not changed.
3. `authenticate`: PBKDF2-HMAC-SHA256, 600,000 iterations for new hashes; legacy `salt$digest` hashes (240,000) still verify and are rehashed after a successful login; constant-time compare.
4. Lockout: 5 failures -> 5-minute lock (audit-logged). Inactive accounts count as failures.
5. Session: `current_user` dict (id, username, display_name, role) in the main window. Privileged actions re-read role/active from SQLite (`require_active_admin`, `SmartPOS.require_admin`); restore requires password re-entry.
6. Logout: now clears current_user, cart, discount and payment input, hides the window, audits, then shows the login dialog (cancel quits).

## Strengths
Strong hashing, live DB revalidation in the service layer, one-time first-admin setup, last-admin protection, audit log.

## Findings and changes
| Finding | Status |
|---|---|
| Locked-account message differed from unknown/wrong-password, revealing the username exists | FIXED: all failures (unknown, wrong password, inactive, locked) return `GENERIC_LOGIN_ERROR`; verified by tests |
| Locked path skipped hashing (timing difference) | FIXED: hash is always computed; timing equality was NOT measured, only the code path made uniform |
| Cart / pages of the previous user stayed alive after logout | FIXED for cart/discount/payment and window visibility; other page widgets (tables) are refreshed on next navigation - not individually audited |
| Sidebar toggle stayed hidden for an admin after a cashier session | FIXED |
| Login UI: plain dialog, no show-password, QMessageBox errors, no busy state | REPLACED by `login_dialog.py` |
| Static lockout notice | Added (same text for everyone, leaks nothing) |

## Open / not verified
- Authentication runs on the GUI thread (about 0.3-0.6 s of hashing); the busy state is painted before it, but the window is unresponsive meanwhile. Not moved to a worker thread.
- No idle timeout / session expiry exists. Not added (behaviour change; owner decision).
- Password-change UI and its wiring to `change_own_password` were not reviewed this session.
- Lockout is per account only; no per-machine throttling. Acceptable for a single shop PC, but an attacker can lock a known account (denial of service) - inherent to this design.
- Password policy is length 8 plus a small common-password list.
- Unexpected exceptions during login are logged with a traceback (no credentials in the message; traceback does not include local variable values).
- Real Windows keyboard/IME behaviour for Arabic input in the login fields was not tested.

## Behaviour changes for existing users
Only the error text on failed login. Credentials, identifiers and reset procedure are unchanged.
