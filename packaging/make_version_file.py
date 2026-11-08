"""Write the Windows version resource for Compressor.exe (PyInstaller --version-file): the name, company,
description, version and copyright that File Properties → Details, Task Manager and uninstallers show.
Usage: python packaging/make_version_file.py OUT.txt"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from compressor import APP_NAME, DESCRIPTION, __version__  # noqa: E402


def version_tuple(version: str) -> tuple[int, int, int, int]:
    parts = [int(p) for p in version.split(".")[:4] if p.isdigit()]
    return tuple(parts + [0] * (4 - len(parts)))  # type: ignore[return-value]


def text(version: str = __version__) -> str:
    v = version_tuple(version)
    strings = {
        "CompanyName": "Lanntern",
        "FileDescription": APP_NAME,            # Task Manager's name for the process
        "FileVersion": version,
        "InternalName": APP_NAME,
        "LegalCopyright": "Copyright (C) 2026 Lanntern. GNU GPL version 3.",
        "OriginalFilename": f"{APP_NAME}.exe",
        "ProductName": APP_NAME,
        "ProductVersion": version,
        "Comments": DESCRIPTION,
    }
    table = ",\n          ".join(f"StringStruct({k!r}, {val!r})" for k, val in strings.items())
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={v}, prodvers={v}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1,
                    subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
          {table}])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""


if __name__ == "__main__":
    out = Path(sys.argv[1] if len(sys.argv) > 1 else "version.txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text(), encoding="utf-8")
    print(f"Wrote {out}")
