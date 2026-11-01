# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[],
    datas=[],  # Never ship operational databases or restore-pending files with the application.
    hiddenimports=['services.accounting', 'services.catalog', 'startup_guard', 'services.audit', 'services.customers', 'services.inventory',
                   'services.sales', 'services.security', 'services.auth', 'services.shifts', 'services.purchasing', 'services.settings',
                   'services.money', 'services.tx', 'services.reporting', 'services.backup'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['pytest', 'ruff', 'PyInstaller', 'tkinter', 'unittest'],  # dev-only; not needed at runtime
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='SmartPOS',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    contents_directory='.',
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='SmartPOS',
)
