# SmartPOS Release Readiness

**EXE verdict: NOT VERIFIED / NOT READY to claim.** No Windows build or runtime test was performed. Only Linux offscreen checks were run.

## Reviewed statically
- `SmartPOS.spec`, `SmartPOS_Smoke.spec`, `build_windows.ps1`, `.github/workflows/*`: read superficially; not executed. New modules (`theme_tokens.py`, `login_dialog.py`, `app_version.py`) are imported normally and should be collected by PyInstaller's import analysis, but this is unverified; check the spec hiddenimports after a real build.
- CI (`ci.yml`) edited: compile list now includes the new modules; the GUI job also runs the full pytest suite offscreen. It has not run.
- Version: sidebar now shows `1.1.0-rc2` from `app_version.py`; `VERSION.txt` and `pyproject.toml` agree. Keep three places in sync manually.

## Blockers / required before release
1. Real Windows build, launch on a clean machine, login, a sale, backup and restore.
2. Visual pass of all screens on Windows fonts (Segoe UI / Arabic shaping).
3. Backup/recovery items listed in SMARTPOS_FINAL_HANDOFF.md.
4. Owner decisions (code signing budget etc.).
