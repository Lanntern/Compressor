"""Docker: build cache, unused images and stopped containers.

Docker owns this data, so it's removed with Docker's own commands: it can't go to the Recycle Bin, and the
confirmation says so. Images count only their unique size (layers shared with images you keep stay).
Volumes hold data such as databases and are never listed. Anything belonging to a project you're working
on, or with a container running, is listed but not pre-selected.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path

from .common import Candidate

GROUP = "Docker"
_UNITS = {"b": 1, "kb": 1000, "mb": 1000 ** 2, "gb": 1000 ** 3, "tb": 1000 ** 4, "pb": 1000 ** 5,
          "kib": 1024, "mib": 1024 ** 2, "gib": 1024 ** 3, "tib": 1024 ** 4}


def parse_size(text: str) -> int:
    """Docker's sizes ("16.02MB", "4.096kB", "0B", "10MB (38%)", "4.1kB (virtual 1.82MB)") in bytes."""
    m = re.match(r"\s*([\d.]+)\s*([a-zA-Z]*)", text or "")
    if not m:
        return 0
    try:
        return int(float(m.group(1)) * _UNITS.get(m.group(2).lower() or "b", 1))
    except ValueError:
        return 0


def _docker() -> str | None:
    return shutil.which("docker")


def _run(args: list[str], timeout: float = 60) -> subprocess.CompletedProcess | None:
    exe = _docker()
    if not exe:
        return None
    try:
        return subprocess.run([exe, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return None


def _lines(proc) -> list[dict]:
    import json

    out = []
    for line in (proc.stdout if proc is not None else "").splitlines():
        line = line.strip()
        if line.startswith("{"):
            try:
                out.append(json.loads(line))
            except ValueError:
                pass
    return out


def available() -> tuple[bool, str]:
    """(running, note): Docker's engine must be up to list or remove anything."""
    if not _docker():
        return False, ""
    proc = _run(["info", "--format", "{{.OperatingSystem}}"], timeout=20)
    if proc is None or proc.returncode != 0:
        return False, "Docker isn't running. Start it to include its images and build cache."
    if "docker desktop" in proc.stdout.lower():
        return True, ("Docker Desktop keeps space it frees inside its own virtual disk, so the drive may not "
                      "show it until Docker Desktop shrinks that disk.")
    return True, ""


def _labels(text: str) -> dict[str, str]:
    out = {}
    for part in (text or "").split(","):
        k, _, v = part.partition("=")
        out[k.strip()] = v.strip()
    return out


def running_projects() -> dict[str, str]:
    """Project folders with a running container (from Compose's working_dir label) → container name."""
    out: dict[str, str] = {}
    for c in _lines(_run(["ps", "--format", "{{json .}}"], timeout=20)):
        wd = _labels(c.get("Labels", "")).get("com.docker.compose.project.working_dir")
        if wd:
            out[os.path.normcase(os.path.abspath(wd))] = c.get("Names", "a container")
    return out


def _created(text: str) -> float:
    """Docker's "2026-11-05 15:42:30 +0000 UTC" as a timestamp (0 if it can't be read)."""
    try:
        return datetime.strptime(" ".join(text.split()[:3]), "%Y-%m-%d %H:%M:%S %z").timestamp()
    except (ValueError, AttributeError):
        try:
            return datetime.strptime(text[:19], "%Y-%m-%d %H:%M:%S").timestamp()
        except (ValueError, TypeError):
            return 0.0


def _item(title: str, size: int, detail: str, ref: dict, warning: str = "") -> Candidate:
    return Candidate(Path("docker") / ref["kind"] / ref.get("id", "all"), size, title=title, detail=detail,
                     group=GROUP, warning=warning, extra={"docker": ref, "location": "Docker"})


def find(active: dict[str, str] | None = None, idle_days: int = 30) -> tuple[list[Candidate], list[str]]:
    """Removable Docker data. ``active``: Compose project name (lower case) → why it counts as in use."""
    ok, note = available()
    if not ok:
        return [], [note] if note else []
    active = dict(active or {})
    notes = ["Docker volumes hold data such as databases and are never listed."]
    if note:
        notes.append(note)
    import json

    proc = _run(["system", "df", "-v", "--format", "{{json .}}"], timeout=120)
    try:
        df = json.loads(proc.stdout) if proc is not None and proc.returncode == 0 else {}
    except ValueError:
        df = {}
    # Projects with a running container count as in use, whatever their files say.
    for c in df.get("Containers") or []:
        if str(c.get("State", "")).lower() == "running":
            proj = _labels(c.get("Labels", "")).get("com.docker.compose.project")
            if proj:
                active.setdefault(proj.lower(), f"its container {c.get('Names', '')} is running")

    def project_of(name: str) -> str | None:
        low = name.lower().rsplit("/", 1)[-1]
        for proj in active:
            if low == proj or low.startswith(proj + "-") or low.startswith(proj + "_"):
                return proj
        return None

    items: list[Candidate] = []
    summary = {r.get("Type"): r for r in _lines(_run(["system", "df", "--format", "{{json .}}"], timeout=60))}
    cache = parse_size(str(summary.get("Build Cache", {}).get("Reclaimable", "")))
    if cache > 0:
        items.append(_item("Build cache", cache, "Layers Docker keeps to speed up builds; rebuilt when needed. "
                           "Removed by Docker (docker builder prune); cache in use by a build stays.",
                           {"kind": "builder"}))
    now = time.time()
    dangling = 0
    for im in df.get("Images") or []:
        if str(im.get("Containers", "0")).strip() not in ("0", ""):
            continue                                     # a container (running or not) uses it
        size = parse_size(str(im.get("UniqueSize") or im.get("Size", "")))
        repo, tag = im.get("Repository", ""), im.get("Tag", "")
        if repo in ("<none>", ""):
            dangling += size
            continue
        if size < 1024 * 1024:
            continue
        warning = ""
        proj = project_of(repo)
        if proj:
            warning = f"Built for {proj}, which is in use: {active[proj]}"
        else:
            created = _created(str(im.get("CreatedAt", "")))
            if created and now - created < idle_days * 86400:
                warning = f"Pulled or built {im.get('CreatedSince', 'recently')}"
        items.append(_item(f"{repo}:{tag}", size, "Image no container uses; pulled or built again when needed. "
                           "Removed by Docker. Only the space no other image shares is counted.",
                           {"kind": "image", "id": im.get("ID", "")}, warning))
    if dangling >= 1024 * 1024:
        items.append(_item("Untagged images", dangling, "Leftovers from rebuilt or re-pulled images; nothing uses "
                           "them. Removed by Docker (docker image prune).", {"kind": "dangling"}))
    for c in df.get("Containers") or []:
        state = str(c.get("State", "")).lower()
        if state not in ("exited", "created", "dead"):
            continue
        size = parse_size(str(c.get("Size", "")))
        labels = _labels(c.get("Labels", ""))
        proj = labels.get("com.docker.compose.project", "").lower()
        warning = "Anything saved inside it (not in a volume) is lost"
        if proj in active:
            warning = f"Part of {proj}, which is in use: {active[proj]}"
        items.append(_item(f"Container {c.get('Names', '')}", size, f"Stopped container from {c.get('Image', '')}; "
                           "Compose or docker run creates it again. Removed by Docker.",
                           {"kind": "container", "id": c.get("ID", "")}, warning))
    return items, notes


def remove(ref: dict) -> tuple[bool, str]:
    kind, ident = ref.get("kind"), ref.get("id", "")
    args = {"builder": ["builder", "prune", "-f"], "dangling": ["image", "prune", "-f"],
            "image": ["image", "rm", ident], "container": ["rm", ident]}.get(kind or "")
    if args is None or (kind in ("image", "container") and not ident):
        return False, "Unknown Docker item"
    proc = _run(args, timeout=600)
    if proc is None:
        return False, "Docker isn't available"
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout).strip().splitlines()
        return False, (msg[-1] if msg else f"docker exited with {proc.returncode}")[:200]
    return True, "Removed by Docker"


__all__ = ["find", "remove", "available", "running_projects", "parse_size", "GROUP"]
