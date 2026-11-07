"""Developer clean-up: folders in code projects that tools recreate, and shared package/IDE caches.

A folder is only suggested when there's proof it's generated: its name *and* the file that makes it
(``node_modules`` next to ``package.json``, ``Pods`` next to ``Podfile``, ``bin``/``obj`` next to a
``.csproj``...) or a signature inside it (``pyvenv.cfg``, a ``CACHEDIR.TAG``, ``CMakeCache.txt``).
Never suggested: anything holding files committed to git, a folder with a repository inside, an
in-source CMake build, or a folder we can't prove is generated (``dist``, ``vendor`` outside PHP...).
Everything goes to the Recycle Bin, so it can be put back from History.
"""

from __future__ import annotations

import fnmatch
import os
import shutil
import subprocess
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from .caches import IS_WINDOWS
from ..formats import human
from .common import Candidate, ProgressFn, ScanResult, cancelled, newest_mtime
from .devrules import (  # noqa: F401 - the tables, used here and by the page
    AMBIGUOUS, CACHEDIR_SIGNATURE, KINDS, MIN_SIZE, RULES, SHARED, Rule, _BUILD_MARKERS, _GRADLE,
    _BY_NAME, _NEVER_ENTER, _NODE_LOCKS, _PM_COMMANDS, _PY_PROJECT, _WARN_RULES, shared_specs,
)


def _matches(names: set[str], patterns: tuple[str, ...]) -> bool:
    for pat in patterns:
        if any(ch in pat for ch in "*?["):
            if any(fnmatch.fnmatchcase(n, pat) for n in names):
                return True
        elif pat in names:
            return True
    return False


def _listing(d: Path) -> list[os.DirEntry] | None:
    try:
        with os.scandir(d) as it:
            return list(it)
    except OSError:
        return None


def _is_link(e: os.DirEntry) -> bool:
    """Symlinks and Windows junctions/mount points: never followed, never removed through."""
    try:
        if e.is_symlink() or (hasattr(e, "is_junction") and e.is_junction()):
            return True
        return bool(getattr(e.stat(follow_symlinks=False), "st_file_attributes", 0) & 0x400)   # REPARSE_POINT
    except OSError:
        return True


@dataclass
class _Found:
    path: Path
    beside: Path               # the folder holding it (where its marker is)
    what: str
    restore: str
    warning: str = ""
    kind: str = "build"


def _signature(d: Path, names: set[str]) -> tuple[str, str, str, str] | None:
    """(what, restore, warning, kind) when ``d`` proves by its own contents that it's generated."""
    if "pyvenv.cfg" in names:
        return "Python virtual environment", "python -m venv and pip install recreate it", "", "venv"
    if "CMakeCache.txt" in names and "CMakeLists.txt" not in names:
        try:
            for line in (d / "CMakeCache.txt").read_text("utf-8", "replace").splitlines():
                if line.startswith("CMAKE_HOME_DIRECTORY:"):
                    src = Path(line.split("=", 1)[1].strip())
                    if src.is_dir() and not _within(src, d):       # an out-of-source build of real sources
                        return "CMake build folder", "cmake recreates it", "", "build"
                    break
        except OSError:
            pass
        return None
    if "meson-private" in names and "meson.build" not in names:
        return "Meson build folder", "meson setup recreates it", "", "build"
    if "CACHEDIR.TAG" in names:
        try:
            with open(d / "CACHEDIR.TAG", "rb") as f:
                if f.read(len(CACHEDIR_SIGNATURE)) == CACHEDIR_SIGNATURE:
                    return "Cache folder", "the tool that made it recreates it", "", "cache"
        except OSError:
            pass
    return None


def _within(p: Path, folder: Path) -> bool:
    try:
        Path(os.path.abspath(p)).relative_to(os.path.abspath(folder))
        return True
    except ValueError:
        return False


def _restore_for(rule: Rule, beside_names: set[str]) -> str:
    if rule.names == ("node_modules",):
        for lock, cmd in _PM_COMMANDS:
            if lock in beside_names:
                return f"{cmd} puts them back"
    return rule.restore


def _scan_projects(roots: list[Path], progress: ProgressFn | None, cancel: threading.Event | None,
                   repos: list[Path] | None = None) -> list[_Found]:
    found: list[_Found] = []
    seen: set[str] = set()
    home_library = Path.home() / "Library"
    stack = [Path(os.path.abspath(r)) for r in roots]
    root_keys = {os.path.normcase(str(r)) for r in stack}
    looked = 0
    while stack:
        if cancelled(cancel):
            break
        d = stack.pop()
        key = os.path.normcase(str(d))
        if key in seen:
            continue
        seen.add(key)
        entries = _listing(d)
        if entries is None:
            continue
        looked += 1
        if progress and looked % 250 == 0:
            progress(f"Looking through projects… {looked:,} folders", looked, 0)
        names = {e.name for e in entries}
        if repos is not None and ".git" in names:
            repos.append(d)
        if key not in root_keys:
            sig = _signature(d, names)
            if sig is not None:
                found.append(_Found(d, d.parent, *sig))
                continue
        for e in entries:
            try:
                if not e.is_dir(follow_symlinks=False) or _is_link(e):
                    continue
            except OSError:
                continue
            p = Path(e.path)
            rule = next((r for r in _BY_NAME.get(e.name, ()) if not r.next_to or _matches(names, r.next_to)), None)
            if rule is not None:
                inner = None
                if rule.inside:
                    inner = _listing(p)
                    if inner is None or not _matches({x.name for x in inner}, rule.inside):
                        rule = None
                if rule is not None:
                    warning = _WARN_RULES.get(e.name, "")
                    if rule.lock and not _matches(names, rule.lock):
                        warning = "No lock file next to it, so a reinstall may get newer versions"
                    found.append(_Found(p, d, rule.what, _restore_for(rule, names), warning, rule.kind))
                    continue
            low = e.name.lower()
            if low in _NEVER_ENTER or e.name.startswith(".") or p == home_library:
                continue
            if IS_WINDOWS:
                try:
                    if e.stat(follow_symlinks=False).st_file_attributes & 0x6:   # hidden or system
                        continue
                except (OSError, AttributeError):
                    continue
            stack.append(p)
    # A virtual environment needs a requirements file to be rebuilt as it was.
    for f in found:
        if f.what == "Python virtual environment":
            beside = {e.name for e in (_listing(f.beside) or [])}
            if not _matches(beside, _PY_PROJECT):
                f.warning = "No requirements file next to it: note which packages it has first"
    return found


# ---------------------------------------------------------------- folders the project's .gitignore ignores

def _ignored_dirs(repo: Path) -> list[Path]:
    """Folders git reports as entirely ignored (nothing in them tracked), from one ``git ls-files`` call."""
    git = shutil.which("git")
    if not git:
        return []
    try:
        proc = subprocess.run([git, "-C", str(repo), "ls-files", "-z", "--others", "--ignored", "--exclude-standard",
                               "--directory", "--no-empty-directory"], capture_output=True, timeout=120,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []
    out = []
    for raw in proc.stdout.split(b"\0"):
        rel = raw.decode("utf-8", "replace")
        if rel.endswith("/") and rel.rstrip("/").rsplit("/", 1)[-1] in AMBIGUOUS:
            out.append(repo / rel.rstrip("/"))
    return out


def _ambiguous_found(repos: list[Path], already: list[_Found]) -> list[_Found]:
    taken = [f.path for f in already]
    found = []
    for repo in repos:
        for p in _ignored_dirs(repo):
            if any(_within(p, t) for t in taken) or not p.is_dir() or p.is_symlink():
                continue
            beside = {e.name for e in (_listing(p.parent) or [])}
            if not _matches(beside, _BUILD_MARKERS):
                continue
            kind = ("coverage" if "coverage" in AMBIGUOUS[p.name].lower() else
                    "cache" if p.name.startswith(".") else "build")
            found.append(_Found(p, p.parent, AMBIGUOUS[p.name], "your .gitignore ignores it and the project's "
                                                                 "build recreates it", "", kind))
            taken.append(p)
    return found


# ---------------------------------------------------------------- installed apps are not projects

def _inside_installed_app(path: Path) -> bool:
    """node_modules and friends inside an installed Electron/NW.js app (…/MyApp/resources/app/…) are part of
    the program: removing them breaks it."""
    for d in list(path.parents)[:6]:
        if d.name.lower() == "resources":
            names = {e.name.lower() for e in (_listing(d) or [])}
            if any(n.endswith(".asar") for n in names) or "app.asar.unpacked" in names:
                return True
            above = {e.name.lower() for e in (_listing(d.parent) or [])}
            if any(n.endswith(".exe") for n in above) or d.parent.name == "Contents":
                return True
    return False


# ---------------------------------------------------------------- what removing a folder really frees

_CLOUD_ONLY = 0x1000 | 0x40000 | 0x400000      # OFFLINE | RECALL_ON_OPEN | RECALL_ON_DATA_ACCESS


@dataclass
class Measure:
    frees: int = 0       # bytes that come back: on this disk, and not shared through a hard link
    total: int = 0       # what the folder's files add up to
    shared: int = 0      # hard-linked elsewhere (pnpm's store, for one): stays on disk
    cloud: int = 0       # cloud-only placeholders (OneDrive...): not on this disk at all
    files: int = 0


def measure(path: Path, cancel: threading.Event | None = None) -> Measure:
    m = Measure()
    # Windows only reports link counts through a full stat (a directory listing says 0). Do that for every file
    # where links are expected (pnpm), and elsewhere for files big enough to matter (most of the bytes).
    full_stat = IS_WINDOWS and ((path / ".pnpm").is_dir() or (path.parent / "pnpm-lock.yaml").exists()
                                or "pnpm" in str(path).lower())
    stack = [path]
    while stack:
        if cancelled(cancel):
            break
        for e in _listing(stack.pop()) or []:
            try:
                if e.is_symlink():
                    continue
                if e.is_dir(follow_symlinks=False):
                    if not _is_link(e):
                        stack.append(Path(e.path))
                    continue
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            m.files += 1
            m.total += st.st_size
            if getattr(st, "st_file_attributes", 0) & _CLOUD_ONLY:
                m.cloud += st.st_size
                continue
            links = st.st_nlink
            if full_stat or (IS_WINDOWS and st.st_size >= 64 * 1024):
                try:
                    links = os.stat(e.path, follow_symlinks=False).st_nlink
                except OSError:
                    pass
            if links > 1:
                m.shared += st.st_size
                continue
            m.frees += st.st_size
    return m


def _size_note(m: Measure) -> str:
    parts = []
    if m.shared > MIN_SIZE:
        parts.append(f"{human(m.shared)} is shared with other folders through hard links (as pnpm does) and stays")
    if m.cloud > MIN_SIZE:
        parts.append(f"{human(m.cloud)} is only in the cloud, not on this PC")
    return f" Frees {human(m.frees)} of {human(m.total)}: " + "; ".join(parts) + "." if parts else ""


# ---------------------------------------------------------------- activity and protection

def _git_dir(project: Path) -> Path | None:
    g = project / ".git"
    if g.is_dir():
        return g
    try:
        text = g.read_text("utf-8", "replace")
    except OSError:
        return None
    if text.startswith("gitdir:"):
        return (project / text.split(":", 1)[1].strip()).resolve()
    return None


def git_activity(project: Path) -> float:
    """When git last saw work here: a commit, checkout, merge, stash or staged change."""
    g = _git_dir(project)
    newest = 0.0
    if g is not None:
        for name in ("logs/HEAD", "index", "HEAD", "ORIG_HEAD", "MERGE_HEAD", "logs/refs/stash"):
            try:
                newest = max(newest, (g / name).stat().st_mtime)
            except OSError:
                pass
    return newest


def project_id(project: Path) -> str:
    """How a protected project is remembered: by its git remote when it has one (so moving or renaming the
    folder doesn't lose the protection), else by its path."""
    g = _git_dir(project)
    if g is not None:
        try:
            text = (g / "config").read_text("utf-8", "replace")
        except OSError:
            text = ""
        section = ""
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("["):
                section = line
            elif section == '[remote "origin"]' and line.replace(" ", "").startswith("url="):
                url = line.split("=", 1)[1].strip()
                return "remote:" + url.rstrip("/").removesuffix(".git").lower()
    return "path:" + os.path.normcase(os.path.abspath(project))


def _protected(path: Path, project: Path, protected: set[str]) -> bool:
    if not protected:
        return False
    if project_id(project) in protected:
        return True
    for p in [path, *path.parents]:
        if "path:" + os.path.normcase(os.path.abspath(p)) in protected:
            return True
    return False


# ---------------------------------------------------------------- git: never touch committed files

def _repo_of(path: Path, stop: set[str]) -> Path | None:
    for parent in path.parents:
        if (parent / ".git").exists():
            return parent
        if os.path.normcase(str(parent)) in stop:
            break
    return None


def _tracked(repo: Path, folders: list[Path]) -> set[Path]:
    """Which of ``folders`` (inside ``repo``) hold files committed to git."""
    git = shutil.which("git")
    if not git:
        return set()
    out: set[Path] = set()
    rels = {f: Path(os.path.relpath(f, repo)).as_posix() for f in folders}
    items = list(rels.items())
    for i in range(0, len(items), 40):
        chunk = items[i:i + 40]
        try:
            proc = subprocess.run([git, "-C", str(repo), "ls-files", "-z", "--", *[r + "/" for _, r in chunk]],
                                  capture_output=True, timeout=60,
                                  creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0:
            continue
        files = [x.decode("utf-8", "replace") for x in proc.stdout.split(b"\0") if x]
        for folder, rel in chunk:
            if any(fp == rel or fp.startswith(rel + "/") for fp in files):
                out.add(folder)
    return out


def _has_repo_inside(path: Path, limit: int = 4000) -> bool:
    """A git repository inside a "build" folder means it isn't one (e.g. a checkout someone put there)."""
    stack, seen = [path], 0
    while stack and seen < limit:
        d = stack.pop()
        for e in _listing(d) or []:
            seen += 1
            if e.name == ".git":
                return True
            try:
                if e.is_dir(follow_symlinks=False) and not _is_link(e) and e.name != "node_modules":
                    stack.append(Path(e.path))
            except OSError:
                pass
    return False


# ---------------------------------------------------------------- the scan

def _ago(days: float) -> str:
    if days < 1:
        return "today"
    if days < 2:
        return "yesterday"
    if days < 45:
        return f"{int(days)} days ago"
    if days < 548:
        months = max(2, round(days / 30.4))
        return f"{months} months ago"
    years = round(days / 365)
    return "a year ago" if years <= 1 else f"{years} years ago"


def find_dev_clutter(roots: list[Path], idle_days: int = 30, *, shared: bool = True,
                     protected: set[str] | frozenset[str] = frozenset(), kinds_off: set[str] | frozenset[str] = frozenset(),
                     names_off: set[str] | frozenset[str] = frozenset(), docker: bool = True,
                     progress: ProgressFn | None = None, cancel: threading.Event | None = None) -> ScanResult:
    """Rebuildable folders in projects under ``roots``, plus shared developer caches.

    Projects untouched for ``idle_days`` (no file changed and no git activity) are pre-selected; ones you've
    worked on lately are listed but left for you to choose. Groups are projects (their repository folder when
    there is one). ``protected``: ``project_id`` values and ``path:`` entries that are never listed.
    ``kinds_off``: KINDS not to list; ``names_off``: folder names not to list (``node_modules``...). Projects
    with something running in them (a terminal, a dev server, a Docker container) count as in use."""
    from . import docker as docker_mod
    from .inuse import busy_projects
    from .caches import find_caches

    result = ScanResult("devclean")
    repos: list[Path] = []
    found = _scan_projects(roots, progress, cancel, repos)
    if cancelled(cancel):
        result.cancelled = True
        return result
    if progress:
        progress("Checking what your .gitignore files mark as build output…", 0, 0)
    found += _ambiguous_found(repos, found)
    switched_off = [f for f in found if f.kind in kinds_off or f.path.name in names_off]
    found = [f for f in found if f not in switched_off]

    stop = {os.path.normcase(str(Path(os.path.abspath(r)).parent)) for r in roots}
    in_apps = [f for f in found if _inside_installed_app(f.path)]
    found = [f for f in found if f not in in_apps]
    by_repo: dict[Path, list[_Found]] = defaultdict(list)
    for f in found:
        repo = _repo_of(f.path, stop)
        if repo is not None:
            by_repo[repo].append(f)
    committed: set[Path] = set()
    for repo, items in by_repo.items():
        if cancelled(cancel):
            break
        if progress:
            progress(f"Checking what's committed in {repo.name}…", 0, 0)
        committed |= _tracked(repo, [f.path for f in items])
    skipped = [f for f in found if f.path in committed]
    found = [f for f in found if f.path not in committed]
    project_of = {f.path: _repo_of(f.path, stop) or f.beside for f in found}
    hidden = [f for f in found if _protected(f.path, project_of[f.path], set(protected))]
    found = [f for f in found if f not in hidden]

    if progress:
        progress("Checking which projects have something running…", 0, 0)
    busy = busy_projects(sorted(set(project_of.values())))
    if docker and "docker" not in kinds_off:
        for wd, name in docker_mod.running_projects().items():
            for proj in set(project_of.values()):
                if os.path.normcase(os.path.abspath(proj)) == wd:
                    busy.setdefault(proj, f"its Docker container {name} is running")

    # Measure (the slow part: these folders hold many small files).
    sizes: dict[Path, Measure | None] = {}
    lock = threading.Lock()
    done = [0]

    def work(f: _Found) -> None:
        if cancelled(cancel):
            return
        m = measure(f.path, cancel)
        nested = _has_repo_inside(f.path)
        with lock:
            sizes[f.path] = None if nested else m
            done[0] += 1
            if progress:
                progress(f"Measuring {done[0]:,} of {len(found):,}: {f.path.name}", done[0], len(found))

    with ThreadPoolExecutor(max_workers=min(8, (os.cpu_count() or 2) * 2)) as pool:
        list(pool.map(work, found))
    if cancelled(cancel):
        result.cancelled = True
        return result

    now = time.time()
    last_work: dict[Path, float] = {}
    heavy = {n for r in RULES for n in r.names} | set(AMBIGUOUS) | {".git", "node_modules"}
    for f in found:
        m = sizes.get(f.path)
        if m is None or m.frees < MIN_SIZE:
            continue
        project = project_of[f.path]
        if project not in last_work:
            last_work[project] = max(newest_mtime(project, skip_names=heavy), git_activity(project))
        idle = (now - last_work[project]) / 86400 if last_work[project] else 1e9
        try:
            title = Path(os.path.relpath(f.path, project)).as_posix()
        except ValueError:
            title = f.path.name
        warning = f.warning
        if project in busy:
            warning = f"In use: {busy[project]}"
        elif not warning and idle < idle_days:
            warning = f"You worked on this project {_ago(idle)}"
        result.items.append(Candidate(
            f.path, m.frees, title=title, detail=f"{f.what}; {f.restore}.{_size_note(m)}", is_dir=True,
            group=str(project), warning=warning,
            extra={"files": m.files, "location": str(f.path), "idle_days": idle, "project": str(project),
                   "project_id": project_id(project), "total": m.total, "kind": f.kind}))
    totals: dict[str, int] = defaultdict(int)
    for c in result.items:
        totals[c.group] += c.size
    result.items.sort(key=lambda c: (-totals[c.group], c.group, -c.size))   # biggest projects first

    if shared and "shared" not in kinds_off and not cancelled(cancel):
        def shared_size(p: Path, c) -> tuple[int, int]:
            m = measure(p, c)
            return m.frees, m.files

        caches = find_caches(progress, cancel, shared_specs(), measure=shared_size)
        for c in caches.items:
            c.extra["kind"] = "shared"
        result.items += [c for c in caches.items if not _protected(c.path, c.path, set(protected))]
    if docker and "docker" not in kinds_off and not cancelled(cancel):
        if progress:
            progress("Asking Docker what it can let go…", 0, 0)
        active: dict[str, str] = {}
        for proj, last in last_work.items():
            reason = busy.get(proj) or (f"you worked on it {_ago((now - last) / 86400)}"
                                         if last and now - last < idle_days * 86400 else "")
            if reason:
                active[_compose_name(proj)] = reason
        for proj, reason in busy.items():
            active.setdefault(_compose_name(proj), reason)
        found_docker, notes = docker_mod.find(active, idle_days)
        for c in found_docker:
            c.extra["kind"] = "docker"
        result.items += found_docker
        result.notes += notes
    if docker and "vdisk" not in kinds_off and not cancelled(cancel):
        from . import vdisk

        if progress:
            progress("Measuring WSL and Docker virtual disks…", 0, 0)
        for c in vdisk.find():
            c.extra["kind"] = "vdisk"
            result.items.append(c)
    if any(f.path in sizes and sizes[f.path] is None for f in found):
        result.notes.append("Folders with a git repository inside were left out.")
    if skipped:
        result.notes.append(f"{len(skipped)} folder{'s' if len(skipped) != 1 else ''} with files committed to "
                            "git were left out.")
    if in_apps:
        result.notes.append(f"{len(in_apps)} folder{'s' if len(in_apps) != 1 else ''} inside installed apps "
                            "were left out.")
    if switched_off:
        result.notes.append(f"{len(switched_off)} folder{'s' if len(switched_off) != 1 else ''} of kinds you "
                            "switched off not shown.")
    if hidden:
        result.notes.append(f"{len(hidden)} folder{'s' if len(hidden) != 1 else ''} in protected projects "
                            "not shown.")
    return result


def _compose_name(project: Path) -> str:
    """The name Docker Compose gives a project by default: its folder name, lower case, letters and digits."""
    import re

    return re.sub(r"[^a-z0-9_-]", "", project.name.lower())


def group_title(group: str, items: list[Candidate]) -> str:
    if group in (SHARED, "Docker", "WSL and Docker virtual disks"):
        return group
    idle = items[0].extra.get("idle_days", 0)
    n = len(items)
    return f"{Path(group).name}  ·  {n} folder{'s' if n != 1 else ''}  ·  changed {_ago(idle)}"


__all__ = ["find_dev_clutter", "shared_specs", "group_title", "RULES", "SHARED"]
