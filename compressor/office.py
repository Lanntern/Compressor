"""Office documents to PDF with the office apps already on this computer.

Microsoft Office does its own formats best, so on Windows Word, Excel or PowerPoint is used when installed
(driven through PowerShell and COM, invisibly, the document opened read-only). Otherwise LibreOffice, on any
system, when installed. Nothing is bundled: without either, Convert says what's needed.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

from . import tools

# Extension -> the Microsoft Office app that opens it.
OFFICE_EXTS = {
    ".doc": "word", ".docx": "word", ".docm": "word", ".dot": "word", ".dotx": "word", ".rtf": "word",
    ".odt": "word", ".wpd": "word",
    ".xls": "excel", ".xlsx": "excel", ".xlsm": "excel", ".xlsb": "excel", ".ods": "excel", ".csv": "excel",
    ".ppt": "powerpoint", ".pptx": "powerpoint", ".pptm": "powerpoint", ".pps": "powerpoint",
    ".ppsx": "powerpoint", ".odp": "powerpoint",
}
_PROG_IDS = {"word": "Word.Application", "excel": "Excel.Application", "powerpoint": "PowerPoint.Application"}

# PowerShell for each app; the paths come in environment variables (no quoting to get wrong).
_SCRIPTS = {
    "word": """$ErrorActionPreference = 'Stop'
$app = New-Object -ComObject Word.Application
try {
  $app.Visible = $false; $app.DisplayAlerts = 0
  $doc = $app.Documents.Open($env:CMP_SRC, $false, $true, $false)
  try { $doc.ExportAsFixedFormat($env:CMP_OUT, 17) } finally { $doc.Close(0) }
} finally { $app.Quit() }""",
    "excel": """$ErrorActionPreference = 'Stop'
$app = New-Object -ComObject Excel.Application
try {
  $app.Visible = $false; $app.DisplayAlerts = $false
  $book = $app.Workbooks.Open($env:CMP_SRC, 0, $true)
  try { $book.ExportAsFixedFormat(0, $env:CMP_OUT) } finally { $book.Close($false) }
} finally { $app.Quit() }""",
    "powerpoint": """$ErrorActionPreference = 'Stop'
$app = New-Object -ComObject PowerPoint.Application
try {
  $show = $app.Presentations.Open($env:CMP_SRC, -1, 0, 0)
  try { $show.SaveAs($env:CMP_OUT, 32) } finally { $show.Close() }
} finally { $app.Quit() }""",
}


def _ms_office(app: str) -> bool:
    if os.name != "nt":
        return False
    try:
        import winreg

        winreg.CloseKey(winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, _PROG_IDS[app] + r"\CLSID"))
        return True
    except OSError:
        return False


def libreoffice() -> str | None:
    found = tools.find_tool("soffice")
    if found:
        return found
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            exe = Path(base) / "LibreOffice" / "program" / "soffice.exe"
            if exe.is_file():
                return str(exe)
    elif Path("/Applications/LibreOffice.app/Contents/MacOS/soffice").is_file():
        return "/Applications/LibreOffice.app/Contents/MacOS/soffice"
    return None


def converter_for(ext: str) -> str:
    """"Microsoft Word" (or Excel, PowerPoint), "LibreOffice", or "" when nothing here can do it."""
    app = OFFICE_EXTS.get(ext.lower(), "word")
    if _ms_office(app):
        return f"Microsoft {app.capitalize() if app != 'powerpoint' else 'PowerPoint'}"
    return "LibreOffice" if libreoffice() else ""


def available() -> bool:
    return any(_ms_office(a) for a in _PROG_IDS) or libreoffice() is not None


def to_pdf(src: Path, dest: Path, cancel: threading.Event | None = None, timeout: float = 600) -> str:
    """``src`` as a PDF at ``dest``. Returns the app that made it."""
    from . import pdftools

    app = OFFICE_EXTS.get(src.suffix.lower(), "word")
    if _ms_office(app):
        env = {**os.environ, "CMP_SRC": str(src.resolve()), "CMP_OUT": str(dest.resolve())}
        proc = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                               "-Command", _SCRIPTS[app]], env=env, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if proc.returncode != 0 or not dest.exists():
            raise RuntimeError(f"{converter_for(src.suffix)} couldn't make the PDF: "
                               f"{(proc.stderr or proc.stdout).strip().splitlines()[-1:] or ['no details']}")
        used = converter_for(src.suffix)
    else:
        soffice = libreoffice()
        if soffice is None:
            raise tools.ToolMissing("LibreOffice")
        work = Path(tempfile.mkdtemp(prefix="compressor-office-"))
        try:
            profile = (work / "profile").as_uri()      # (its own profile: works while LibreOffice is open too)
            proc = subprocess.run([soffice, "--headless", "--norestore", "--nolockcheck",
                                   f"-env:UserInstallation={profile}", "--convert-to", "pdf", "--outdir",
                                   str(work / "out"), str(src)], capture_output=True, text=True, timeout=timeout,
                                  stdin=subprocess.DEVNULL, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            made = work / "out" / (src.stem + ".pdf")
            if not made.exists():
                raise RuntimeError("LibreOffice couldn't make the PDF: "
                                   + ((proc.stderr or proc.stdout).strip().splitlines() or ["no details"])[-1])
            shutil.move(str(made), str(dest))
        finally:
            shutil.rmtree(work, ignore_errors=True)
        used = "LibreOffice"
    if pdftools.page_count(dest) < 1:
        raise RuntimeError("The PDF has no pages")
    return used


__all__ = ["OFFICE_EXTS", "to_pdf", "available", "converter_for", "libreoffice"]
