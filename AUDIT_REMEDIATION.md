# Audit remediation (supersedes the 1.0.0 status table)

The 1.0.0 status table was re-verified, not trusted. The current, evidence-based status of every finding
(C-01…M-04 plus 14 new findings) is in `docs/FINAL_AUDIT_REPORT.md`; test evidence in `docs/TEST_REPORT.md`;
database decisions in `docs/DATABASE_RECOVERY_REPORT.md`.

## Still open (do not skip)
1. N-11 Product price edit/delete and category/supplier CRUD are guarded only in the UI — move behind admin-checked services.
2. N-12 Make the 04:00 business-day cutoff configurable.
3. Run `pytest` and `ruff` for real (CI does it on push); this release was verified with a stand-in runner.
4. Build and smoke-test on Windows (`docs/BUILD_WINDOWS.md` checklist) — the GUI edits were never executed.
5. Owner decisions: which database snapshot (conflicting histories), classify 2 UNKNOWN returns, review unknown-cost lines and the
   P001 margin, cashier-returns policy.
6. Screens for damage write-off / stocktake; purchase returns; supplier balances; parent/variant catalogue; split `app.py`.
