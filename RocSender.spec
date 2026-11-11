# -*- mode: python ; coding: utf-8 -*-
from pathlib import Path

root = Path(SPECPATH)
native = root / "build" / "Release"
dlls = [(str(native / name), "native")
        for name in ("roc.dll", "libc++.dll", "libunwind.dll")]

gui = Analysis(
    [str(root / "desktop_app.py")], pathex=[str(root)],
    binaries=dlls, datas=[(str(root / "config.example.toml"), "."),
                         (str(root / "LICENSE"), "."),
                         (str(root / "assets" / "roc-sender.ico"), "assets"),
                         (str(root / "licenses"), "licenses")],
    hiddenimports=[], hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=[], noarchive=False,
)
worker = Analysis(
    [str(root / "worker_entry.py")], pathex=[str(root)],
    binaries=[], datas=[], hiddenimports=[], hookspath=[], hooksconfig={},
    runtime_hooks=[], excludes=["PySide6"], noarchive=False,
)
gui_exe = EXE(PYZ(gui.pure), gui.scripts, [], exclude_binaries=True,
              name="RocSender", debug=False, bootloader_ignore_signals=False,
              strip=False, upx=False, console=False,
              icon=str(root / "assets" / "roc-sender.ico"))
worker_exe = EXE(PYZ(worker.pure), worker.scripts, [], exclude_binaries=True,
                 name="RocSenderWorker", debug=False, bootloader_ignore_signals=False,
                 strip=False, upx=False, console=True)

# Qt 6 uses the Windows ICU API (e.g. ucnv_open). Conda's identically named
# icuuc.dll exports versioned symbols (ucnv_open_78), so collecting it shadows
# the OS DLL and prevents QtCore from loading on an otherwise clean Windows.
# Neither sender nor Python requires Conda ICU; use the Windows implementation.
def without_conda_icu(binaries):
    return [entry for entry in binaries
            if Path(entry[0]).name.lower() not in {"icuuc.dll", "icudt78.dll"}]

app = COLLECT(gui_exe, worker_exe, without_conda_icu(gui.binaries), gui.datas,
              without_conda_icu(worker.binaries), worker.datas, strip=False, upx=False,
              name="RocSender")
