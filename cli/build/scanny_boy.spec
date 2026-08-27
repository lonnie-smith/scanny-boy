# PyInstaller spec for producing a standalone `scanny-boy` binary
# to embed in the macOS app bundle (see scripts/build-cli.sh).
#
# Build with:
#   pyinstaller cli/build/scanny_boy.spec

a = Analysis(
    ["../src/scanny_boy/cli.py"],
    pathex=["../src"],
    hiddenimports=[],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="scanny-boy",
    console=True,
)
