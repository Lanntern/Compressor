"""Is anything Wick bundles out of date, or known to be vulnerable?

* The command-line tools pinned in scripts/fetch_tools.py, against each upstream project's newest release.
* The Python libraries pinned in requirements.txt, against known vulnerabilities (pip-audit / OSV).

Prints a Markdown report. With --issue (in CI, with GH_TOKEN set) it opens a GitHub issue, or updates the
open one, when something needs attention; nothing changes on its own: updating stays a reviewed commit.

Usage:  python scripts/check_updates.py [--issue]
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import fetch_tools  # noqa: E402

# tool -> (repository, tag pattern capturing the version). jpegli is pinned to the last libjxl release that
# ships cjpegli on Windows, so it isn't compared.
UPSTREAM = {
    "ffmpeg": ("https://github.com/FFmpeg/FFmpeg", r"^n(\d+\.\d+(?:\.\d+)?)$"),
    "libjxl": ("https://github.com/libjxl/libjxl", r"^v(\d+\.\d+\.\d+)$"),
    "gifsicle": ("https://github.com/kohler/gifsicle", r"^v(\d+\.\d+(?:\.\d+)?)$"),
    "pandoc": ("https://github.com/jgm/pandoc", r"^(\d+\.\d+(?:\.\d+)*)$"),   # (also pinned in wick/addons.py)
}
TITLE = "Bundled components: updates or security fixes available"


def _key(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split("."))


def latest(repo: str, pattern: str) -> str | None:
    try:
        out = subprocess.run(["git", "ls-remote", "--tags", "--refs", repo], capture_output=True, text=True,
                             timeout=120, check=True).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    versions = [m.group(1) for line in out.splitlines()
                if (m := re.match(pattern, line.rsplit("refs/tags/", 1)[-1]))]
    return max(versions, key=_key) if versions else None


def tool_findings() -> list[str]:
    pinned: dict[str, set[str]] = {}
    for d in [*fetch_tools.WINDOWS, fetch_tools.LINUX_LIBJXL, fetch_tools.LINUX_PANDOC, fetch_tools.LINUX_FFMPEG]:
        if d.held:
            yield f"- Note: {d.tool} {d.version} is held back on purpose: {d.held}."
            continue
        pinned.setdefault(d.tool, set()).add(d.version)
        if d.sha256 is None:
            yield f"- **{d.tool} {d.version}** has no recorded checksum in `scripts/fetch_tools.py`."
    for tool, (repo, pattern) in UPSTREAM.items():
        newest = latest(repo, pattern)
        if newest is None:
            yield f"- Couldn't read {tool}'s releases from {repo}."
            continue
        for v in sorted(pinned.get(tool, ())):
            if _key(newest) > _key(v):
                yield f"- **{tool}**: we pin {v}, upstream's newest is **{newest}** ({repo}/releases)."


def audit_findings() -> list[str]:
    if not shutil.which("pip-audit"):
        return ["- pip-audit isn't installed, so Python libraries weren't checked."]
    proc = subprocess.run(["pip-audit", "-r", str(ROOT / "requirements.txt"), "--require-hashes", "--disable-pip",
                           "--format", "json"], capture_output=True, text=True, timeout=600)
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return [f"- pip-audit didn't run cleanly: {(proc.stderr or proc.stdout).strip()[:300]}"]
    out = []
    for dep in data.get("dependencies", []):
        for v in dep.get("vulns", []):
            fix = ", ".join(v.get("fix_versions") or []) or "no fixed version yet"
            out.append(f"- **{dep['name']} {dep['version']}**: {v['id']} (fixed in {fix})")
    return out


def report() -> tuple[str, bool]:
    tools = list(tool_findings())
    libs = audit_findings()
    lines = ["Found by the weekly check (`scripts/check_updates.py`).", ""]
    lines += ["### Command-line tools", *(tools or ["- All pinned tools match upstream's newest release."]), ""]
    lines += ["### Python libraries", *(libs or ["- No known vulnerabilities in requirements.txt."]), ""]
    lines += ["To update: change the version, URL and checksum in `scripts/fetch_tools.py`, or regenerate "
              "requirements.txt (command at its top), then let CI test the build."]
    attention = any(not x.startswith(("- Couldn't", "- Note:")) for x in tools) or any(libs)
    return "\n".join(lines), attention


def file_issue(body: str) -> None:
    found = subprocess.run(["gh", "issue", "list", "--state", "open", "--search", f'in:title "{TITLE}"',
                            "--json", "number,title"], capture_output=True, text=True, check=True).stdout
    existing = [i for i in json.loads(found or "[]") if i["title"] == TITLE]
    if existing:
        subprocess.run(["gh", "issue", "edit", str(existing[0]["number"]), "--body", body], check=True)
        print(f"Updated issue #{existing[0]['number']}")
    else:
        subprocess.run(["gh", "issue", "create", "--title", TITLE, "--body", body], check=True)
        print("Opened an issue")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--issue", action="store_true", help="open or update a GitHub issue when something is found")
    a = ap.parse_args()
    body, attention = report()
    print(body)
    if a.issue and attention:
        file_issue(body)
    return 0


if __name__ == "__main__":
    sys.exit(main())
