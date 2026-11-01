# -*- mode: python ; coding: utf-8 -*-
# CI-ONLY: freezes scripts/smoke_test.py with the same dependency set as SmartPOS.spec so the GUI
# smoke test can run against PyInstaller-bundled Qt/SQLite. NOT part of the shipped release.
a = Analysis(
    ['scripts/smoke_test.py'],
    pathex=['.'],
    binaries=[], datas=[],
    hiddenimports=['app', 'database', 'ui_theme', 'services.accounting', 'services.catalog', 'startup_guard', 'services.audit', 'services.customers',
                   'services.inventory', 'services.sales', 'services.security', 'services.auth', 'services.shifts',
                   'services.purchasing', 'services.settings', 'services.money', 'services.tx',
                   'services.reporting', 'services.backup'],
    hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['pytest', 'ruff', 'PyInstaller', 'tkinter', 'unittest'],
    noarchive=False, optimize=0,
)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name='SmartPOS_SmokeTest', debug=False,
          strip=False, upx=False, console=True, contents_directory='.')
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='SmartPOS_SmokeTest')
