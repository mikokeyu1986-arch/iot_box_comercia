# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['C:\\Users\\Miko win\\Documents\\odoo\\custom_addons\\iot_box_comercia\\gui_app.py'],
    pathex=[],
    binaries=[],
    datas=[('C:\\Users\\Miko win\\Documents\\odoo\\custom_addons\\iot_box_comercia\\web', 'web'), ('C:\\Users\\Miko win\\Documents\\odoo\\custom_addons\\iot_box_comercia\\certs', 'certs'), ('C:\\Users\\Miko win\\Documents\\odoo\\custom_addons\\iot_box_comercia\\runtime_config.json', '.')],
    hiddenimports=['pystray', 'pywebview'],
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
    name='gui_app',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['C:\\Users\\Miko win\\Documents\\odoo\\custom_addons\\iot_box_comercia\\assets\\iotbox-icon.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='gui_app',
)
