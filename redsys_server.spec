# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['D:/odoo/iot_box_comercia/redsys/server/main.py'],
    pathex=[],
    binaries=[],
    datas=[('D:/odoo/iot_box_comercia/redsys', 'redsys')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='redsys_server',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['D:/odoo/iot_box_comercia/assets/iotbox-icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='redsys_server',
)
