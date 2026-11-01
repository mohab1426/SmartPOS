# Building on Windows

Requires Windows 10/11 x64 and Python 3.11 or 3.12 (64-bit). Run `.\build_windows.ps1` (or the "Windows release build" GitHub Action).
`.\build_windows.ps1` — creates `.venv`, installs pinned `requirements-dev.txt`, **runs the tests (build stops if
any fail)**, then runs `pyinstaller --clean --noconfirm SmartPOS.spec`. Output: `dist\SmartPOS\SmartPOS.exe`
(one-folder build). No database is bundled; data lives in `%LOCALAPPDATA%\SmartPOS`.

**Status (1.1.0-rc2):** the script runs ruff, pytest, a GUI smoke test (source and PyInstaller-frozen), a launch check of
SmartPOS.exe, a no-database-bundled check, then writes `release\SmartPOS_Windows_Release.zip`. As of this package it has been
validated on Linux only (same specs, trial build, headless Qt) - **the PowerShell script and workflow have not yet been run on
Windows.** Run the GitHub Action once and read its log before relying on the EXE. Receipt printing, barcode scanner, Arabic
rendering and window-resize behaviour still need a manual check on the target PC.
