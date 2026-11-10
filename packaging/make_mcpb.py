"""Builds Wick's Claude Desktop extension (``Wick.mcpb``): manifest.json, the launcher that starts
the installed app's MCP server (``wick-mcp.exe``), and the icon.

    python packaging/make_mcpb.py LAUNCHER_EXE OUT.mcpb

The tools listed in the manifest are taken from the server itself, so the descriptions people (and the
directory's reviewers) read are exactly what the server reports.
"""

from __future__ import annotations

import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

REPO = "https://github.com/Lanntern/Wick"


def manifest() -> dict:
    from wick import APP_NAME, __version__
    from wick.mcp_server import DEFAULT_MAX_FILES, Server

    tools = Server([], out=None, forbidden=[]).tools()
    return {
        "manifest_version": "0.3",
        "name": "wick",
        "display_name": APP_NAME,
        "version": __version__,
        "description": "Find and reclaim space taken by photos, videos, music and documents on your PC, with every change "
                       "approved by you and undoable.",
        "long_description": (
            "Requires the Wick app for Windows (free, open source): " + REPO + "\n\n"
            "Lets Claude look for photos, videos, music and documents that can be stored smaller, in folders you choose, and "
            "compress them with Wick's encoders (lossless JPEG XL by default, so every photo stays "
            "bit-for-bit recoverable).\n\n"
            "**Safe by design**\n"
            "- Claude can only use the folders you pick below; system and program folders are always off limits.\n"
            "- Planning changes nothing. Before any file is changed, or a run is undone, Wick asks you in "
            "its own window, which Claude can't answer. No answer means no change.\n"
            "- Originals go to the Recycle Bin, never deleted permanently, and every run can be undone from "
            "Wick's History (or by asking Claude).\n"
            "- Everything stays on your computer. Claude sees the names and sizes of the files it plans, and "
            "nothing is sent anywhere else."),
        "author": {"name": "Lanntern", "url": REPO},
        "repository": {"type": "git", "url": REPO + ".git"},
        "homepage": REPO,
        "documentation": REPO + "/blob/main/docs/mcp.md",
        "support": REPO + "/issues",
        "icon": "icon.png",
        "server": {
            "type": "binary",
            "entry_point": "server/wick-mcp.exe",
            "mcp_config": {
                "command": "${__dirname}/server/wick-mcp.exe",
                "args": ["--keep-originals=${user_config.keep_originals}",
                         "--max-files=${user_config.max_files}", "--", "${user_config.allowed_directories}"],
                "env": {"WICK_EXE": "${user_config.wick_exe}"},
            },
        },
        "tools": [{"name": t["name"], "description": t["description"]} for t in tools],
        "tools_generated": False,
        "keywords": ["compression", "photos", "video", "disk space", "jpeg xl", "storage", "windows"],
        "license": "GPL-3.0-only",
        "privacy_policies": [REPO + "/blob/main/PRIVACY.md"],
        "compatibility": {"platforms": ["win32"]},
        "user_config": {
            "allowed_directories": {
                "type": "directory", "multiple": True, "required": True,
                "title": "Folders Wick may use",
                "description": "Claude can only look at and compress files inside these folders (and their "
                               "subfolders). System and program folders are always refused."},
            "keep_originals": {
                "type": "boolean", "default": False,
                "title": "Keep originals",
                "description": "Save compressed copies next to the originals instead of moving the originals to "
                               "the Recycle Bin. Saves no space, but never touches your files."},
            "max_files": {
                "type": "number", "default": DEFAULT_MAX_FILES, "min": 1, "max": 10000,
                "title": "Most files per run",
                "description": "Larger plans are refused, so Claude works through big folders a part at a time."},
            "wick_exe": {
                "type": "file", "required": False,
                "title": "Wick.exe location (portable version only)",
                "description": "Only needed for the portable zip; the installed app is found automatically."},
        },
    }


def build(launcher: Path, out: Path) -> Path:
    icon = ROOT / "docs" / "icon.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("manifest.json", json.dumps(manifest(), indent=2, ensure_ascii=False) + "\n")
        z.write(launcher, "server/wick-mcp.exe")
        z.write(icon, "icon.png")
        z.write(ROOT / "LICENSE", "LICENSE")
    return out


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    print(build(Path(sys.argv[1]), Path(sys.argv[2])))
