# ucf.spec — PyInstaller build spec for standalone ucf binaries
# Lives at repo root alongside uc.py
# Manual build: pyinstaller ucf.spec

from PyInstaller.building.build_main import Analysis, PYZ, EXE

a = Analysis(
    ['uc.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[
        'requests',
        'requests.adapters',
        'requests.auth',
        'requests.exceptions',
        'urllib3',
        'urllib3.util.retry',
        'urllib3.util.ssl_',
        'certifi',
        'charset_normalizer',
        'idna',
    ],
    hookspath=[],
    runtime_hooks=[],
    excludes=[
        'tkinter', '_tkinter',
        'matplotlib', 'numpy', 'scipy', 'pandas',
        'PIL', 'Pillow',
        'PyQt5', 'PyQt6', 'wx',
        'unittest', 'doctest', 'pdb',
        'email', 'mailbox',
        'http.server', 'xmlrpc', 'ftplib',
        'imaplib', 'poplib', 'smtplib',
        'telnetlib', 'nntplib', 'sndhdr',
        'curses', 'readline',
        'sqlite3', 'csv', 'plistlib',
        'xml', 'html',
        'distutils', 'setuptools', 'pkg_resources',
        'cryptography', 'OpenSSL',
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='ucf',
    debug=False,
    strip=True,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,
    onefile=True,
)
