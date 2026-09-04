# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the packaged command-line program.

One-directory mode wrapped in `ScannyBoyCLI.app` (`docs/IMPLEMENTATION_PLAN.md`
section 5.2). One-file mode is deliberately not used: the app launches `probe`
often, and one-file mode would unpack the whole distribution on every call.

The `datas` and `hiddenimports` below are not decoration. Each one fixes a
failure that happens only at run time in the packaged program, never during
the build, so the packaged checks in `packaging_test.py` are what catch a
regression.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules, copy_metadata

CLI_DIR = Path(SPECPATH).resolve().parent  # noqa: F821 — PyInstaller injects SPECPATH
SRC_DIR = CLI_DIR / "src"

datas = [
    # The vetted ICC profiles, loaded through `importlib.resources` so the
    # same code works in a checkout and in the bundle (section 3.4).
    (str(SRC_DIR / "scanny_boy" / "resources" / "ScannyBoy-Linear-ProPhoto-v1.icc"), "scanny_boy/resources"),
    (str(SRC_DIR / "scanny_boy" / "resources" / "ScannyBoy-Density-ProPhoto-v1.icc"), "scanny_boy/resources"),
    # The Alembic migration scripts for the library database. `db.py`
    # locates them at the bundle root when frozen (`sys._MEIPASS`), which
    # this destination provides.
    (str(SRC_DIR / "scanny_boy" / "library" / "migrations"), "migrations"),
]

# `tifftools` reads its own package metadata at import; without this the
# packaged program dies immediately with `PackageNotFoundError` (section 5.2).
datas += copy_metadata("tifftools")
# Same failure mode for our own distribution: `manifest.py` and
# `tiff_writer.py` ask `importlib.metadata` for the Scanny Boy version, which
# reaches the manifest and every TIFF's `Software` tag.
datas += copy_metadata("scanny-boy")

# `imagecodecs` loads its codecs through delayed imports PyInstaller cannot
# see. Without them the horizontal predictor is missing and the first TIFF
# write fails with `DelayedImportError: could not import name 'delta_encode'`.
# Narrowing this to individual submodules was tried and does not work; do not
# trim it without rebuilding and rerunning the packaged checks (section 5.2).
hiddenimports = collect_submodules("imagecodecs")

# The library database stack. Alembic resolves its migration templates and
# SQLAlchemy its sqlite dialect through imports PyInstaller's static
# analysis does not follow, so both are collected explicitly — a bundle
# without them starts fine and dies on the first `roll` command.
hiddenimports += collect_submodules("alembic")
hiddenimports += collect_submodules("mako")
hiddenimports += collect_submodules("sqlalchemy.dialects.sqlite")

analysis = Analysis(  # noqa: F821
    [str(SRC_DIR / "scanny_boy" / "__main__.py")],
    pathex=[str(SRC_DIR)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Development-only dependencies. Excluding them keeps the bundle from
        # carrying a test runner and a second copy of PyInstaller.
        "pytest",
        "_pytest",
        "pytest_cov",
        "coverage",
        "PyInstaller",
        "setuptools",
        "tkinter",
        # SciPy's and NumPy's test suites are pulled in by PyInstaller's
        # scipy hook's package scan; excluding them holds the bundle down
        # without touching what `import scipy.optimize` needs at run time.
        "scipy.tests",
        "numpy.tests",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(analysis.pure)  # noqa: F821

exe = EXE(  # noqa: F821
    pyz,
    analysis.scripts,
    [],
    exclude_binaries=True,
    name="scanny-boy",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    # A console program: the app drives it through pipes, and both stdout
    # (the JSON event stream) and stderr (human logs) must work there.
    console=True,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

collect = COLLECT(  # noqa: F821
    exe,
    analysis.binaries,
    analysis.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="scanny-boy",
)

app = BUNDLE(  # noqa: F821
    collect,
    name="ScannyBoyCLI.app",
    icon=None,
    bundle_identifier="com.lonniesmith.scanny-boy.cli",
    info_plist={
        # A helper, never a Dock icon or a menu bar (section 5.2).
        "LSBackgroundOnly": True,
        "CFBundleName": "ScannyBoyCLI",
        "CFBundleDisplayName": "Scanny Boy CLI",
        "CFBundleExecutable": "scanny-boy",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "0.1.0",
        "CFBundleVersion": "0.1.0",
        "NSHighResolutionCapable": True,
    },
)
