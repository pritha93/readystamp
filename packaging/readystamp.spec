# PyInstaller build recipe. Run from the repo root:  pyinstaller packaging/readystamp.spec
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

datas = collect_data_files("readystamp")  # fonts and web screen files
hiddenimports = (
    collect_submodules("readystamp")
    + collect_submodules("uvicorn")
    + ["multipart", "python_multipart"]
)

a = Analysis(
    ["launcher.py"],
    pathex=[".."],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    name="ReadyStamp",
    console=True,
    upx=False,
)
