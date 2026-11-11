"""Clean-up tools: scanning finds the right things; actions and undo round-trip."""

import os
import tarfile
import time
import zipfile
from pathlib import Path

import pytest

from wick import journal
from wick.cleanup import actions, appcompress, archives, caches, duplicates, empties, installers
from wick.engine import undo_run


def _write(p: Path, data: bytes, age_days: float = 0) -> Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    if age_days:
        t = time.time() - age_days * 86400
        os.utime(p, (t, t))
    return p


def test_duplicates_hardlink_and_undo(tmp_path):
    payload = os.urandom(300_000)
    a = _write(tmp_path / "Photos" / "a.jpg", payload, age_days=10)
    b = _write(tmp_path / "Backup" / "copy of a.jpg", payload)
    _write(tmp_path / "other.bin", os.urandom(300_000))           # same size, different content
    link = tmp_path / "Photos" / "hardlinked.jpg"
    os.link(a, link)                                               # already shares storage: not a duplicate
    res = duplicates.find_duplicates([tmp_path], min_size=1)
    assert [c.path.name for c in res.items] == ["copy of a.jpg"]
    item = res.items[0]
    assert os.stat(item.keeper).st_ino == os.stat(a).st_ino        # the oldest copy is kept
    assert item.size == len(payload)

    outcomes, run_id = actions.apply(res.items, actions.HARDLINK, "duplicates", tmp_path)
    assert outcomes[0].ok, outcomes[0].message
    assert os.stat(b).st_ino == os.stat(a).st_ino and b.read_bytes() == payload
    ok, failed = undo_run(run_id)
    assert (ok, failed) == (1, 0)
    assert os.stat(b).st_ino != os.stat(a).st_ino and b.read_bytes() == payload


def test_duplicates_keep_rules(tmp_path):
    data = os.urandom(200_000)
    old = _write(tmp_path / "x" / "deep" / "old.dat", data, age_days=5)
    new = _write(tmp_path / "new.dat", data)
    assert duplicates.find_duplicates([tmp_path], min_size=1, keep="newest").items[0].keeper == new
    assert duplicates.find_duplicates([tmp_path], min_size=1, keep="shortest_path").items[0].keeper == new
    res = duplicates.find_duplicates([tmp_path], min_size=1, keep="folder", prefer=tmp_path / "x")
    assert res.items[0].keeper == old


def test_duplicates_trash_and_undo(tmp_path):
    data = os.urandom(150_000)
    _write(tmp_path / "a.bin", data, age_days=1)
    b = _write(tmp_path / "b.bin", data)
    res = duplicates.find_duplicates([tmp_path], min_size=1)
    outcomes, run_id = actions.apply(res.items, actions.TRASH, "duplicates", tmp_path)
    assert outcomes[0].ok and not b.exists()
    assert undo_run(run_id) == (1, 0)
    assert b.read_bytes() == data
    run = journal.get_run(run_id)
    assert run.files[0].undone


def _make_tree(base: Path) -> None:
    _write(base / "readme.txt", b"hello" * 1000)
    _write(base / "sub" / "data.bin", os.urandom(5000))


@pytest.mark.parametrize("fmt", ["zip", "7z", "tar.gz"])
def test_extracted_archives(tmp_path, fmt):
    src = tmp_path / "Downloads" / "project"
    _make_tree(src)
    arc = tmp_path / "Downloads" / f"project.{fmt}"
    files = [p for p in src.rglob("*") if p.is_file()]
    if fmt == "zip":
        with zipfile.ZipFile(arc, "w") as z:
            for p in files:
                z.write(p, p.relative_to(src).as_posix())
    elif fmt == "7z":
        import py7zr

        with py7zr.SevenZipFile(arc, "w") as z:
            for p in files:
                z.write(p, p.relative_to(src).as_posix())
    else:
        with tarfile.open(arc, "w:gz") as t:
            for p in files:
                t.add(p, p.relative_to(src).as_posix())
    res = archives.find_extracted_archives([tmp_path])
    assert len(res.items) == 1 and res.items[0].selected and not res.items[0].warning, res.items
    # A changed file means the archive is not redundant any more.
    (src / "readme.txt").write_bytes(b"HELLO" * 1000)
    res = archives.find_extracted_archives([tmp_path])
    assert len(res.items) == 1 and not res.items[0].selected and res.items[0].warning


def test_archive_with_top_folder_extracted_next_to_it(tmp_path):
    d = tmp_path / "dl"
    _write(d / "repo-main" / "a.txt", b"a" * 100)
    with zipfile.ZipFile(d / "repo-main.zip", "w") as z:
        z.write(d / "repo-main" / "a.txt", "repo-main/a.txt")
    res = archives.find_extracted_archives([d])
    assert len(res.items) == 1 and res.items[0].selected


def test_installers(tmp_path):
    _write(tmp_path / "SteamSetup.exe", b"x" * 1000, age_days=90)
    _write(tmp_path / "new_installer.msi", b"x" * 1000)                 # too recent
    _write(tmp_path / "ubuntu.iso", b"x" * 1000, age_days=200)
    res = installers.find_installers([tmp_path], older_than_days=30)
    by = {c.path.name: c for c in res.items}
    assert set(by) == {"SteamSetup.exe", "ubuntu.iso"}
    assert by["SteamSetup.exe"].selected and not by["ubuntu.iso"].selected
    assert installers.match_installed("SteamSetup.exe", ["Steam", "Discord"]) == "Steam"
    assert installers.match_installed("random_tool.exe", ["Steam"]) is None
    assert installers.match_installed("MicrosoftTeamsSetup.exe", ["Microsoft Edge", "Microsoft Teams"]) \
        == "Microsoft Teams"


def test_installers_ignore_games_and_programs(tmp_path):
    pe = b"MZ" + b"\0" * 4000
    # Real installers: by name, or by an installer framework's signature inside.
    _write(tmp_path / "vlc-3.0.20-win64.exe", pe + b"Nullsoft.NSIS.exehead" + b"\0" * 100, age_days=90)
    _write(tmp_path / "python-3.12.0-amd64.exe", pe + b".wixburn" + b"\0" * 100, age_days=90)
    _write(tmp_path / "Firefox Setup 120.exe", pe, age_days=90)
    _write(tmp_path / "tool.msi", b"x" * 1000, age_days=90)
    # Not installers: a portable game/app, an uninstaller, and a game folder with its DLLs.
    _write(tmp_path / "Celeste.exe", pe + b"some game data", age_days=90)
    _write(tmp_path / "unins000.exe", pe + b"Inno Setup", age_days=90)
    _write(tmp_path / "Games" / "Hades" / "Hades.exe", pe + b"Inno Setup", age_days=90)
    _write(tmp_path / "Games" / "Hades" / "fmod.dll", b"MZ", age_days=90)
    _write(tmp_path / "Games" / "Hades" / "setup.exe", pe, age_days=90)
    res = installers.find_installers([tmp_path], older_than_days=30)
    found = {c.path.name for c in res.items}
    assert found == {"vlc-3.0.20-win64.exe", "python-3.12.0-amd64.exe", "Firefox Setup 120.exe", "tool.msi"}


def test_empty_items_are_really_empty(tmp_path):
    from wick.cleanup import actions

    _write(tmp_path / "hidden-only" / ".settings", b"x" * 5000)       # hidden 5 KB file: not empty
    (tmp_path / "nested" / "deeper").mkdir(parents=True)
    _write(tmp_path / "nested" / "deeper" / ".cache", b"x" * 10)      # hidden file deep down: not empty
    (tmp_path / "truly" / "empty").mkdir(parents=True)
    res = empties.find_empty([tmp_path])
    folders = {c.path.name for c in res.items if c.is_dir}
    assert folders == {"truly"}

    # Something appears in it after the scan: it's left alone.
    _write(tmp_path / "truly" / "empty" / "new.txt", b"x")
    outcomes, _ = actions.apply([c for c in res.items if c.is_dir], actions.TRASH, "empty", tmp_path)
    assert not outcomes[0].ok and (tmp_path / "truly" / "empty" / "new.txt").exists()


def test_empty_items(tmp_path):
    (tmp_path / "a" / "b" / "c").mkdir(parents=True)
    _write(tmp_path / "keep" / "file.txt", b"x")
    _write(tmp_path / "pkg" / "__init__.py", b"")
    _write(tmp_path / "zero.log", b"")
    res = empties.find_empty([tmp_path])
    names = {(c.path.name, c.is_dir, c.selected) for c in res.items}
    assert ("a", True, True) in names                     # only the top of the empty tree
    assert not any(n[0] in ("b", "c") for n in names)
    assert ("zero.log", False, False) in names            # zero-byte files are never pre-selected
    assert not any(n[0] == "__init__.py" for n in names)


def _age_tree(root: Path, days: float) -> None:
    t = time.time() - days * 86400
    for dirpath, dirs, files in os.walk(root):
        for n in dirs + files:
            os.utime(os.path.join(dirpath, n), (t, t))


def test_developer_folders(tmp_path):
    from wick.cleanup import devclean

    big = os.urandom(2 * 1024 * 1024)
    old = tmp_path / "old-app"                       # idle Node project with a lock file: pre-selected
    _write(old / "package.json", b"{}")
    _write(old / "yarn.lock", b"")
    _write(old / "src" / "index.js", b"code")
    _write(old / "node_modules" / "lib" / "big.js", big)
    new = tmp_path / "new-app"                       # worked on today, no lock file: listed, not pre-selected
    _write(new / "package.json", b"{}")
    _write(new / "node_modules" / "x.js", big)
    rust = tmp_path / "crate"
    _write(rust / "Cargo.toml", b"[package]")
    _write(rust / "target" / "CACHEDIR.TAG", devclean.CACHEDIR_SIGNATURE + b"\n")
    _write(rust / "target" / "debug" / "big", big)
    _write(tmp_path / "notes" / "target" / "plan.txt", big)       # "target" without Cargo.toml: user data
    _write(tmp_path / "site" / "build" / "page.html", big)        # "build" with no build tool beside it
    venv = tmp_path / "tool"
    _write(venv / "requirements.txt", b"requests")
    _write(venv / "env-3.12" / "pyvenv.cfg", b"home = /usr/bin")  # any name: found by its pyvenv.cfg
    _write(venv / "env-3.12" / "lib" / "big.so", big)
    insrc = tmp_path / "insource"                                 # in-source CMake build: never
    _write(insrc / "CMakeLists.txt", b"project(x)")
    _write(insrc / "CMakeCache.txt", f"CMAKE_HOME_DIRECTORY:INTERNAL={insrc}\n".encode())
    _write(insrc / "main.c", big)
    cm = tmp_path / "cmakeproj"
    _write(cm / "CMakeLists.txt", b"project(x)")
    _write(cm / "out" / "CMakeCache.txt", f"CMAKE_HOME_DIRECTORY:INTERNAL={cm}\n".encode())
    _write(cm / "out" / "obj.o", big)
    ios = tmp_path / "iosapp"
    _write(ios / "Podfile", b"pod 'X'")
    _write(ios / "Podfile.lock", b"")
    _write(ios / "Pods" / "X" / "lib.a", big)
    for p in (old, rust, tmp_path / "notes", tmp_path / "site", venv, insrc, cm, ios):
        _age_tree(p, 200)

    res = devclean.find_dev_clutter([tmp_path], idle_days=30, shared=False, docker=False)
    by = {c.path: c for c in res.items}
    assert set(by) == {old / "node_modules", new / "node_modules", rust / "target", venv / "env-3.12",
                       cm / "out", ios / "Pods"}
    assert by[old / "node_modules"].selected and "yarn install" in by[old / "node_modules"].detail
    assert not by[new / "node_modules"].selected and by[new / "node_modules"].warning
    assert by[venv / "env-3.12"].selected and by[ios / "Pods"].selected and by[cm / "out"].selected
    assert all(c.group for c in res.items)

    outcomes, run_id = actions.apply([by[old / "node_modules"]], actions.TRASH, "devclean", tmp_path)
    assert outcomes[0].ok and not (old / "node_modules").exists() and (old / "src" / "index.js").exists()
    assert undo_run(run_id) == (1, 0) and (old / "node_modules" / "lib" / "big.js").exists()


@pytest.mark.skipif(not __import__("shutil").which("git"), reason="git not installed")
def test_developer_folders_skip_committed_files(tmp_path):
    import subprocess

    from wick.cleanup import devclean

    repo = tmp_path / "repo"
    _write(repo / "package.json", b"{}")
    _write(repo / "package-lock.json", b"{}")
    _write(repo / "node_modules" / "vendored.js", os.urandom(2 * 1024 * 1024))
    git = ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t"]
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    subprocess.run(git + ["add", "-A"], check=True)
    subprocess.run(git + ["commit", "-qm", "x"], check=True)
    res = devclean.find_dev_clutter([tmp_path], idle_days=0, shared=False, docker=False)
    assert not res.items and any("committed" in n for n in res.notes)


def test_trash_refuses_what_windows_would_delete_permanently(tmp_path, monkeypatch):
    from wick import fileops

    f = _write(tmp_path / "big.bin", os.urandom(4096))
    monkeypatch.setattr(fileops, "recycle_bin_capacity", lambda p: 1000)   # a tiny Recycle Bin
    with pytest.raises(fileops.TooBigForTrash):
        fileops.move_to_trash(f)
    assert f.exists()
    out = actions.trash_item(__import__("wick.cleanup.common", fromlist=["Candidate"]).Candidate(f, 4096),
                             journal.RunWriter(tmp_path, {}), "x")
    assert not out.ok and "Too big" in out.message and f.exists()


def test_cache_specs_measure(tmp_path):
    d = tmp_path / "SomeApp" / "Cache"
    _write(d / "blob", os.urandom(2 * 1024 * 1024))
    spec = caches.CacheSpec("Some cache", "Apps", (str(tmp_path / "SomeApp" / "Cache"),), "Rebuilt automatically.")
    res = caches.find_caches(spec_list=[spec])
    assert len(res.items) == 1 and res.items[0].is_dir and res.items[0].size >= 2 * 1024 * 1024
    outcomes, run_id = actions.apply(res.items, actions.TRASH, "caches", tmp_path)
    assert outcomes[0].ok and not d.exists()
    assert undo_run(run_id) == (1, 0) and (d / "blob").exists()


@pytest.mark.skipif(not appcompress.supported(), reason="NTFS compression is Windows-only")
def test_app_compression_roundtrip(tmp_path):
    folder = tmp_path / "Game"
    for i in range(20):
        _write(folder / f"data{i}.txt", (b"compressible game data " * 4096))
    logical, before, count = appcompress.measure(folder)
    assert appcompress.run_compact(folder) == 0
    _, after, _ = appcompress.measure(folder)
    assert after < before * 0.5, (before, after)
    assert appcompress.run_compact(folder, undo=True) == 0
    _, restored, _ = appcompress.measure(folder)
    assert restored >= logical * 0.9
    assert (folder / "data0.txt").read_bytes() == b"compressible game data " * 4096


def _git(repo: Path, *args: str) -> None:
    import subprocess

    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *args], check=True,
                   capture_output=True)


@pytest.mark.skipif(not __import__("shutil").which("git"), reason="git not installed")
def test_developer_ambiguous_folders_need_gitignore(tmp_path):
    """dist/build/coverage are only listed when the project's .gitignore ignores them."""
    from wick.cleanup import devclean

    big = os.urandom(2 * 1024 * 1024)
    repo = tmp_path / "web"
    _write(repo / "package.json", b"{}")
    _write(repo / "package-lock.json", b"{}")
    _write(repo / ".gitignore", b"dist/\ncoverage/\n")
    _write(repo / "dist" / "app.js", big)
    _write(repo / "coverage" / "lcov.info", big)
    _write(repo / "out" / "release-notes.pdf", big)          # not ignored: could be anything
    _write(repo / "packages" / "ui" / "package.json", b"{}")
    _write(repo / "packages" / "ui" / "dist" / "ui.js", big)  # ignored by the same pattern, in a monorepo
    _git(repo, "init", "-q")
    _git(repo, "add", "package.json", "package-lock.json", ".gitignore", "packages/ui/package.json")
    _git(repo, "commit", "-qm", "x")
    _age_tree(repo, 200)
    res = devclean.find_dev_clutter([tmp_path], idle_days=30, shared=False, docker=False)
    found = {c.path for c in res.items}
    assert found == {repo / "dist", repo / "coverage", repo / "packages" / "ui" / "dist"}


def test_developer_skips_installed_apps_and_protected_projects(tmp_path):
    from wick.cleanup import devclean

    big = os.urandom(2 * 1024 * 1024)
    app = tmp_path / "Tools" / "SomeEditor"                 # an installed Electron app, not a project
    _write(app / "SomeEditor.exe", b"MZ")
    _write(app / "resources" / "app" / "package.json", b"{}")
    _write(app / "resources" / "app" / "node_modules" / "dep.js", big)
    mine = tmp_path / "mine"
    _write(mine / "package.json", b"{}")
    _write(mine / "package-lock.json", b"{}")
    _write(mine / "node_modules" / "dep.js", big)
    _write(mine / ".git" / "config", b'[remote "origin"]\n\turl = https://github.com/me/mine.git\n')
    _write(mine / ".git" / "HEAD", b"ref: refs/heads/main\n")
    _age_tree(tmp_path, 200)
    res = devclean.find_dev_clutter([tmp_path], idle_days=30, shared=False, docker=False)
    assert {c.path for c in res.items} == {mine / "node_modules"}
    assert any("installed apps" in n for n in res.notes)
    pid = devclean.project_id(mine)
    assert pid == "remote:https://github.com/me/mine"          # survives moving or renaming the folder
    res = devclean.find_dev_clutter([tmp_path], idle_days=30, shared=False, docker=False, protected={pid})
    assert not res.items and any("protected" in n for n in res.notes)


def test_developer_activity_includes_git_and_sizes_are_honest(tmp_path):
    from wick.cleanup import devclean

    big = os.urandom(3 * 1024 * 1024)
    proj = tmp_path / "proj"
    _write(proj / "package.json", b"{}")
    _write(proj / "pnpm-lock.yaml", b"")
    _write(proj / "node_modules" / "own.js", big)
    store = _write(tmp_path / "store" / "shared.js", big)         # pnpm-style: a hard link into a store
    os.link(store, proj / "node_modules" / "linked.js")
    _write(proj / ".git" / "HEAD", b"ref: refs/heads/main\n")
    _age_tree(proj, 200)
    _write(proj / ".git" / "logs" / "HEAD", b"commit yesterday")  # files are old, but you committed today
    res = devclean.find_dev_clutter([proj.parent], idle_days=30, shared=False, docker=False)
    (c,) = [c for c in res.items if c.path == proj / "node_modules"]
    assert c.size == len(big)                                    # the hard-linked half stays on disk
    assert "shared" in c.detail and "pnpm install" in c.detail
    assert not c.selected and "today" in c.warning


def test_free_just_these_from_the_trash(tmp_path):
    d = tmp_path / "proj" / "node_modules"
    _write(d / "a.js", os.urandom(4096))
    keep = _write(tmp_path / "other.txt", b"stays in the bin")
    item = __import__("wick.cleanup.common", fromlist=["Candidate"]).Candidate(d, 4096, is_dir=True)
    other = __import__("wick.cleanup.common", fromlist=["Candidate"]).Candidate(keep, 16)
    outcomes, _ = actions.apply([item, other], actions.TRASH, "devclean", tmp_path)
    assert all(o.ok for o in outcomes) and not d.exists()
    from wick import trash_restore

    assert trash_restore.find_trashed(d, outcomes[0].trashed_at) is not None
    count, freed, problems = actions.empty_from_trash(outcomes[:1])
    assert (count, freed, problems) == (1, 4096, [])
    assert trash_restore.find_trashed(d, outcomes[0].trashed_at) is None
    assert trash_restore.find_trashed(keep, outcomes[1].trashed_at) is not None   # the rest stays in the bin


def test_developer_kinds_can_be_switched_off(tmp_path):
    from wick.cleanup import devclean

    big = os.urandom(2 * 1024 * 1024)
    p = tmp_path / "app"
    _write(p / "package.json", b"{}")
    _write(p / "package-lock.json", b"{}")
    _write(p / "node_modules" / "x.js", big)
    _write(p / ".parcel-cache" / "c", big)
    _write(p / "venv" / "pyvenv.cfg", b"home = /usr")
    _write(p / "venv" / "lib.so", big)
    _write(p / "requirements.txt", b"x")
    _age_tree(p, 200)
    every = {c.path.name for c in devclean.find_dev_clutter([tmp_path], shared=False, docker=False).items}
    assert every == {"node_modules", ".parcel-cache", "venv"}
    res = devclean.find_dev_clutter([tmp_path], shared=False, docker=False, kinds_off={"venv"},
                                    names_off={".parcel-cache"})
    assert {c.path.name for c in res.items} == {"node_modules"}
    assert any("switched off" in n for n in res.notes)
    assert all(k in devclean.KINDS for k in {r.kind for r in devclean.RULES})


def test_developer_projects_in_use(tmp_path):
    """Something running in a project (a terminal, a dev server) counts as in use, whatever its file dates."""
    import subprocess
    import sys

    from wick.cleanup import devclean, inuse

    p = tmp_path / "served"
    _write(p / "package.json", b"{}")
    _write(p / "package-lock.json", b"{}")
    _write(p / "node_modules" / "x.js", os.urandom(2 * 1024 * 1024))
    _age_tree(p, 200)
    # A grandchild (the app's own child processes don't count), working in the project folder.
    launcher = subprocess.run([sys.executable, "-c", (
        "import subprocess, sys; "
        "q = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], cwd=sys.argv[1], "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL); "
        "print(q.pid)"), str(p)], capture_output=True, text=True, check=True)
    pid = int(launcher.stdout.strip())
    try:
        assert "running in it" in inuse.busy_projects([p]).get(p, "")
        (c,) = devclean.find_dev_clutter([tmp_path], idle_days=30, shared=False, docker=False).items
        assert not c.selected and c.warning.startswith("In use:")
    finally:
        try:
            os.kill(pid, 9)
        except OSError:
            pass
    assert inuse.busy_projects([tmp_path / "elsewhere"]) == {}


def test_docker_sizes():
    from wick.cleanup.docker import parse_size

    assert parse_size("16.02MB") == 16_020_000 and parse_size("4.096kB") == 4096
    assert parse_size("10MB (38%)") == 10_000_000 and parse_size("4.1kB (virtual 1.82MB)") == 4100
    assert parse_size("0B") == 0 and parse_size("") == 0 and parse_size("1.5GiB") == int(1.5 * 1024 ** 3)


def _docker_builds_linux_images() -> bool:
    import shutil
    import subprocess

    if not shutil.which("docker"):
        return False
    try:
        proc = subprocess.run(["docker", "info", "--format", "{{.OSType}}"], capture_output=True, text=True,
                              timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "linux"


@pytest.mark.skipif(not _docker_builds_linux_images(), reason="needs a running Docker engine for Linux images")
def test_docker_unused_images_and_containers(tmp_path):
    import subprocess

    from wick.cleanup import docker

    def build(tag: str) -> None:
        d = tmp_path / tag.replace(":", "_")
        _write(d / "blob", os.urandom(3 * 1024 * 1024))
        (d / "Dockerfile").write_text("FROM scratch\nCOPY blob /blob\n")
        subprocess.run(["docker", "build", "-q", "-t", tag, str(d)], check=True, capture_output=True)

    names = ("cmptest-old:1", "cmptest-web:1", "cmptest-used:1")
    try:
        for n in names:
            build(n)
        subprocess.run(["docker", "create", "--name", "cmptest-stopped", "cmptest-used:1", "/x"], check=True,
                       capture_output=True)
        items, notes = docker.find({"cmptest": "you worked on it today"}, idle_days=0)
        by = {c.title: c for c in items}
        assert "cmptest-old:1" in by and by["cmptest-old:1"].size >= 3_000_000
        assert by["cmptest-web:1"].warning                         # built for a project in use
        assert "cmptest-used:1" not in by                          # a container uses it
        assert not by["Container cmptest-stopped"].selected        # may hold data: never pre-selected
        assert any("volumes" in n for n in notes)
        outcomes, _ = actions.apply([by["Container cmptest-stopped"], by["cmptest-old:1"]], actions.TRASH,
                                    "devclean", tmp_path)
        assert all(o.ok for o in outcomes), [o.message for o in outcomes]
        left = {c.title for c in docker.find({}, idle_days=0)[0]}
        assert "cmptest-old:1" not in left and "Container cmptest-stopped" not in left
    finally:
        subprocess.run(["docker", "rm", "-f", "cmptest-stopped"], capture_output=True)
        subprocess.run(["docker", "rmi", "-f", *names], capture_output=True)


def test_app_compression_notices_directstorage_and_updates(tmp_path):
    """DirectStorage games are recognized; files an update added after compressing are found."""
    folder = tmp_path / "Game"
    _write(folder / "bin" / "dstorage.dll", b"MZ" * 100)
    old = _write(folder / "old.pak", os.urandom(300_000), age_days=10)
    seen: set[str] = set()
    appcompress.measure(folder, seen=seen)
    assert "dstorage.dll" in seen
    since = time.time() - 86400
    assert appcompress.uncompressed_since(folder, since) == 0          # nothing changed since
    _write(folder / "patch.pak", os.urandom(400_000))                  # an update
    assert appcompress.uncompressed_since(folder, since) >= 400_000
    assert old.exists()
    # History tells which folders this app compressed (undone ones don't count).
    w = journal.RunWriter(tmp_path, {"tool": "appcompress"})
    w.add(journal.FileRecord(original=str(folder), output=str(folder), original_size=1, output_size=0,
                             kind="cleanup", method="LZX", reversible=True, trashed_at=since, mode="compact"))
    w.close()
    assert (folder, since) in [(p, t) for p, t in appcompress.compressed_folders()]


@pytest.mark.skipif(not appcompress.supported(), reason="NTFS compression is Windows-only")
def test_app_compression_again_after_an_update(tmp_path):
    folder = tmp_path / "Game"
    for i in range(10):
        _write(folder / f"data{i}.txt", (b"compressible game data " * 4096))
    item = appcompress.Candidate(folder, 0, title="Game", is_dir=True)
    outcomes, _ = actions.apply([item], actions.COMPACT, "appcompress", tmp_path)
    assert outcomes[0].ok
    time.sleep(1.1)
    for i in range(10):                                               # an update brings new files
        _write(folder / f"update{i}.txt", (b"new compressible data " * 8192))
    done, notes = appcompress.recompress_updated(min_bytes=100_000)
    assert [f for f, _ in done] == [folder] and done[0][1] > 0, notes
    assert appcompress.uncompressed_since(folder, time.time() - 60) == 0


def test_windows_cleanup_script_uses_windows_tools():
    from wick.cleanup import winclean

    ps = winclean.script(["windows_old", "update_cleanup", "delivery_optimization", "memory_dumps"])
    assert "Previous Installations" in ps and "System error memory dump files" in ps
    assert f"cleanmgr.exe -ArgumentList '/sagerun:{winclean.SAGESET}'" in ps
    assert ps.count("Remove-ItemProperty") == ps.count("Set-ItemProperty")      # Disk Cleanup left as it was
    assert "/StartComponentCleanup" in ps and "/ResetBase" not in ps             # updates stay uninstallable
    assert "Delete-DeliveryOptimizationCache" in ps
    assert not __import__("re").search(r"Remove-Item(?!Property)|\brd\b|del ", ps)  # nothing deleted by hand
    assert "cleanmgr" not in winclean.script(["update_cleanup"])
    more = winclean.script(["old_drivers", "compact_os"])
    assert "Device Driver Packages" in more and "/compactos:always" in more


def test_old_driver_versions_are_measured(tmp_path, monkeypatch):
    from wick.cleanup import winclean

    repo = tmp_path / "System32" / "DriverStore" / "FileRepository"
    for i, age in enumerate((300, 200, 10)):                              # three versions of one driver
        d = _write(repo / f"nv_dispi.inf_amd64_{i:04x}" / "nvlddmkm.sys", os.urandom(100_000)).parent
        os.utime(d, (time.time() - age * 86400,) * 2)
    _write(repo / "usb.inf_amd64_0001" / "usb.sys", os.urandom(50_000))   # only one version: stays
    monkeypatch.setenv("SystemRoot", str(tmp_path))
    assert winclean._old_driver_bytes() == 200_000                        # the two older versions


def test_items_made_of_many_files_go_to_the_trash(tmp_path):
    files = [_write(tmp_path / "Temp" / f"old{i}.tmp", os.urandom(1000)) for i in range(3)]
    item = __import__("wick.cleanup.common", fromlist=["Candidate"]).Candidate(
        tmp_path / "Temp", 3000, is_dir=True, extra={"paths": [str(f) for f in files]})
    outcomes, run_id = actions.apply([item], actions.TRASH, "winclean", tmp_path)
    assert outcomes[0].ok and outcomes[0].freed == 3000 and not any(f.exists() for f in files)
    assert (tmp_path / "Temp").is_dir()                                        # the folder itself stays
    assert undo_run(run_id) == (3, 0) and all(f.exists() for f in files)


@pytest.mark.skipif(os.name != "nt", reason="Windows only")
def test_windows_leftovers_scan():
    from wick.cleanup import winclean

    res = winclean.find()
    assert not res.cancelled
    for c in res.items:
        assert c.group in (winclean.SYSTEM, winclean.ACCOUNT)
        assert c.extra.get("system") or c.extra.get("paths") is not None


def _takeout_fixture(tmp_path: Path) -> tuple[list[Path], dict]:
    """Two archives (a .zip and a .tgz) with Google's naming quirks."""
    import io
    import json as _json
    import tarfile as _tar
    import zipfile as _zip

    def meta(ts: int, title: str) -> bytes:
        return _json.dumps({"title": title, "photoTakenTime": {"timestamp": str(ts)},
                            "creationTime": {"timestamp": "1700000000"}}).encode()

    tmp_path.mkdir(parents=True, exist_ok=True)
    beach = os.urandom(5000)
    long_name = "IMG_20190704_181512345_HDR_with_a_long_name.jpg"
    files = {
        "Photos from 2019/IMG_1.JPG": beach,
        "Photos from 2019/IMG_1.JPG.supplemental-metadata.json": meta(1562000000, "IMG_1.JPG"),
        "Photos from 2019/IMG_1-edited.JPG": os.urandom(4000),                      # shares IMG_1's JSON
        "Photos from 2019/IMG_2(1).JPG": os.urandom(3000),
        "Photos from 2019/IMG_2.JPG.supplemental-metadata(1).json": meta(1562100000, "IMG_2.JPG"),
        f"Photos from 2019/{long_name}": os.urandom(3500),
        "Photos from 2019/IMG_20190704_181512345_HDR_with_a.json": meta(1562200000, long_name),
        "Summer Trip/IMG_1.JPG": beach,                                             # album copy
        "Summer Trip/IMG_1.JPG.supplemental-metadata.json": meta(1562000000, "IMG_1.JPG"),
        "Summer Trip/metadata.json": b'{"title": "Summer Trip"}',
    }
    part2 = {"Photos from 2020/VID_3.mp4": os.urandom(6000),
             "Photos from 2020/VID_3.mp4.supplemen.json": meta(1590000000, "VID_3.mp4")}
    a = tmp_path / "takeout-20240101T000000Z-001.zip"
    with _zip.ZipFile(a, "w") as z:
        # The album comes first in the archive: its copy must still end up as a link.
        for rel in sorted(files, key=lambda r: not r.startswith("Summer")):
            z.writestr(f"Takeout/Google Photos/{rel}", files[rel])
    b = tmp_path / "takeout-20240101T000000Z-002.tgz"
    with _tar.open(b, "w:gz") as t:
        for rel, data in part2.items():
            info = _tar.TarInfo(f"Takeout/Google Photos/{rel}")
            info.size = len(data)
            t.addfile(info, io.BytesIO(data))
    return [a, b], {**files, **part2}


def test_google_takeout_merge(tmp_path):
    from wick.cleanup import takeout

    sources, files = _takeout_fixture(tmp_path / "downloads")
    assert takeout.find_sources([tmp_path / "downloads"]) == sources
    res = takeout.find([tmp_path / "downloads"])
    assert {c.extra["photos"] for c in res.items} == {5, 1}
    out = tmp_path / "Photos"
    r = takeout.merge(sources, out)
    assert (r.copied, r.linked, r.undated) == (5, 1, 0)
    year = out / "Photos from 2019"
    assert (year / "IMG_1.JPG").read_bytes() == files["Photos from 2019/IMG_1.JPG"]     # byte-identical
    assert os.path.samefile(year / "IMG_1.JPG", out / "Summer Trip" / "IMG_1.JPG")      # album: no extra space
    dates = {p.name: int(p.stat().st_mtime) for p in out.rglob("*") if p.is_file()}
    assert dates["IMG_1.JPG"] == 1562000000 and dates["IMG_1-edited.JPG"] == 1562000000
    assert dates["IMG_2(1).JPG"] == 1562100000                                         # "(1)" moved
    assert dates["IMG_20190704_181512345_HDR_with_a_long_name.jpg"] == 1562200000      # cut-short JSON
    assert dates["VID_3.mp4"] == 1590000000                                            # .tgz, "supplemen"
    assert not list(out.rglob("*.json"))
    again = takeout.merge(sources, out)                                                # resumes
    assert again.copied == 0 and again.already == 6
    only = takeout.merge(sources, tmp_path / "Originals", albums="skip", edited="original")
    assert only.removed_edited == 1 and not (tmp_path / "Originals" / "Summer Trip").exists()


def test_google_takeout_cannot_write_outside_output(tmp_path):
    """A crafted archive ("../", absolute paths, drive letters) never writes outside the chosen folder."""
    import zipfile as _zip

    from wick.cleanup import takeout

    src = tmp_path / "in" / "takeout-evil.zip"
    src.parent.mkdir()
    with _zip.ZipFile(src, "w") as z:
        z.writestr("Takeout/Google Photos/../../../escaped.jpg", b"x" * 100)
        z.writestr("Takeout/Google Photos/Album/../../escaped2.jpg", b"x" * 100)
        z.writestr("Takeout/Google Photos/C:/escaped3.jpg", b"x" * 100)
        z.writestr("Takeout/Google Photos//Photos from 2019/./ok.jpg", b"y" * 100)
    out = tmp_path / "deep" / "out"
    takeout.merge([src], out)
    written = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*.jpg"))
    assert written == ["deep/out/C_/escaped3.jpg", "deep/out/Photos from 2019/ok.jpg"]
    assert takeout._photos_rel("Takeout/Google Photos/../x.jpg") is None


def test_creative_and_chat_caches(tmp_path):
    """Lightroom previews but never Smart Previews; old catalog backups beyond the newest two; Telegram's
    cache folders but not its login data."""
    lr = tmp_path / "Pictures" / "Lightroom"
    _write(lr / "Photos Previews.lrdata" / "a.lrprev", os.urandom(2 * 1024 * 1024))
    _write(lr / "Photos Smart Previews.lrdata" / "b.dng", os.urandom(2 * 1024 * 1024))
    spec = caches.CacheSpec("Lightroom Classic previews", "Creative apps", (str(lr / "*Previews.lrdata"),), "x",
                            "lightroom", exclude=("*smart previews.lrdata",))
    got = [c.path.name for c in caches.find_caches(spec_list=[spec]).items]
    assert got == ["Photos Previews.lrdata"]
    for i, day in enumerate(("2024-01-01 1200", "2024-02-01 1200", "2024-03-01 1200", "2024-04-01 1200")):
        d = _write(lr / "Backups" / day / "Photos.zip", os.urandom(1024 * 1024 + 10)).parent
        os.utime(d, (time.time() - (10 - i) * 86400,) * 2)
    old = caches.lightroom_backups(lr)
    assert sorted(c.path.name for c in old) == ["2024-01-01 1200", "2024-02-01 1200"]
    tdata = tmp_path / "Telegram Desktop" / "tdata"
    _write(tdata / "user_data" / "cache" / "0" / "blob", os.urandom(2 * 1024 * 1024))
    _write(tdata / "key_datas", b"login")
    spec = caches.CacheSpec("Telegram media cache", "Chat apps", (str(tdata / "user_data" / "cache"),), "x",
                            "telegram")
    (c,) = caches.find_caches(spec_list=[spec]).items
    assert c.path == tdata / "user_data" / "cache"
    windows_specs = {s.name for s in caches.specs()} if os.name == "nt" else set()
    if windows_specs:
        assert {"Adobe media cache", "WhatsApp media", "Lightroom Classic previews"} <= windows_specs


def test_large_files_lists_the_biggest_first(tmp_path):
    from wick.cleanup import largefiles

    (tmp_path / "Videos").mkdir()
    (tmp_path / ".hidden").mkdir()
    for name, mb in (("Videos/old.mp4", 3), ("disk.iso", 5), ("notes.txt", 0), (".hidden/secret.bin", 9),
                     ("Videos/small.mov", 1)):
        with open(tmp_path / name, "wb") as f:
            f.truncate(mb * 1024 * 1024 + 1)
    r = largefiles.find_large_files([tmp_path], 2)
    assert [c.path.name for c in r.items] == ["disk.iso", "old.mp4"]          # biggest first; hidden and small out
    assert not any(c.selected for c in r.items)                               # never pre-selected
    assert r.items[0].detail.startswith("Disk image")
    only = largefiles.find_large_files([tmp_path], 2, "videos")
    assert [c.path.name for c in only.items] == ["old.mp4"]


def test_files_are_what_their_contents_say(tmp_path):
    from wick import journal
    from wick.cleanup import actions, extensions
    from wick.engine import undo_record
    from wick.formats import PNG, sniff, wrong_extension
    from wick.scanner import scan
    from wick.settings import Settings
    from PIL import Image

    Image.new("RGB", (40, 30), "red").save(tmp_path / "photo.jpg", "PNG")      # a PNG called .jpg
    Image.new("RGB", (40, 30), "blue").save(tmp_path / "fine.jpeg", "JPEG")    # .jpeg = .jpg: fine
    (tmp_path / "clip.mov").write_bytes(b"\0\0\0\x20ftypisom" + b"\0" * 40)      # MP4 called .mov: same family
    (tmp_path / "song.mp3").write_bytes(b"fLaC" + b"\0" * 60)                   # FLAC called .mp3
    assert sniff(tmp_path / "photo.jpg") == (PNG, ".png")
    assert wrong_extension(tmp_path / "photo.jpg") == ".png"
    assert wrong_extension(tmp_path / "fine.jpeg") is None and wrong_extension(tmp_path / "clip.mov") is None

    entry = next(e for e in scan(tmp_path, Settings(min_image_kb=0)).entries if e.path.name == "photo.jpg")
    assert entry.kind == PNG and entry.real_ext == ".png"                       # handled as what it is

    found = extensions.find_wrong_extensions([tmp_path])
    assert sorted(c.path.name for c in found.items) == ["photo.jpg", "song.mp3"]
    outcomes, run_id = actions.apply(found.items, actions.RENAME, "extensions", tmp_path)
    assert all(o.ok for o in outcomes) and (tmp_path / "photo.png").exists() and (tmp_path / "song.flac").exists()
    run = journal.load_run(journal.runs_dir() / f"{run_id}.jsonl")
    for rec in run.files:
        assert undo_record(run_id, rec)[0]
    assert (tmp_path / "photo.jpg").exists() and not (tmp_path / "photo.png").exists()


def test_cloud_only_files_are_never_opened(tmp_path, monkeypatch):
    """Files that are only in the cloud (OneDrive, iCloud...) are left out without being read: reading them, even
    their first bytes, makes the cloud app download them. Checked on the scans that walk folders themselves."""
    from wick import formats
    from wick.cleanup import extensions, largefiles
    from wick.gui import thumbs

    for name, extra in (("cloud.jpg", 1), ("here.jpg", 0)):          # PNGs called .jpg, 3 MB; told apart by size
        (tmp_path / name).write_bytes(b"\x89PNG\r\n\x1a\n" + b"\0" * (3_000_000 + extra))
    is_cloud = lambda st: st.st_size == 3_000_009    # noqa: E731  (Windows listings give no inode to go by)
    monkeypatch.setattr(extensions, "cloud_only", is_cloud)
    monkeypatch.setattr(largefiles, "cloud_only", is_cloud)
    monkeypatch.setattr("wick.cleanup.common.cloud_only", is_cloud)
    opened: list[str] = []
    real_sniff = formats.sniff
    monkeypatch.setattr(formats, "sniff", lambda p, head=None: (opened.append(p.name), real_sniff(p, head))[1])
    found = extensions.find_wrong_extensions([tmp_path])
    assert [c.path.name for c in found.items] == ["here.jpg"] and opened == ["here.jpg"]
    assert [c.path.name for c in largefiles.find_large_files([tmp_path], 2).items] == ["here.jpg"]
    monkeypatch.setattr(thumbs, "_own_thumbnail", lambda *a: opened.append("decoded") or b"png")
    assert thumbs.make_thumbnail(tmp_path / "cloud.jpg", "png", 64) is None and "decoded" not in opened


def test_wrong_extensions_are_read_side_by_side(tmp_path, monkeypatch):
    """Many files: all are checked (in parallel), and the results are the same as one at a time."""
    from wick.cleanup import extensions
    from PIL import Image

    for i in range(60):
        Image.new("RGB", (8, 8), "red").save(tmp_path / f"p{i}.jpg", "PNG" if i % 3 == 0 else "JPEG")
    seen = []
    monkeypatch.setattr(extensions, "wrong_extension",
                        lambda p, real=extensions.wrong_extension: (seen.append(p.name), real(p))[1])
    found = extensions.find_wrong_extensions([tmp_path])
    assert len(seen) == 60 and sorted(c.path.name for c in found.items) == sorted(f"p{i}.jpg" for i in range(0, 60, 3))


def test_broken_files_are_found(tmp_path):
    import zipfile

    from wick.cleanup import broken
    from PIL import Image

    Image.effect_noise((300, 200), 40).convert("RGB").save(tmp_path / "fine.jpg", quality=90)
    data = (tmp_path / "fine.jpg").read_bytes()
    (tmp_path / "half.jpg").write_bytes(data[: len(data) // 2])               # an interrupted copy
    (tmp_path / "zeros.png").write_bytes(b"\0" * 20000)                       # space reserved, never filled
    (tmp_path / "renamed.jpg").write_bytes(b"%PDF-1.4\n%%EOF\n")              # another type: not "broken"
    with zipfile.ZipFile(tmp_path / "fine.zip", "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("a.txt", "hello " * 5000)
    zdata = (tmp_path / "fine.zip").read_bytes()
    (tmp_path / "cut.zip").write_bytes(zdata[:-40])
    (tmp_path / "cut.pdf").write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\n")
    (tmp_path / "fine.pdf").write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")
    r = broken.find_broken([tmp_path])
    found = {c.path.name: c for c in r.items}
    assert set(found) == {"half.jpg", "zeros.png", "cut.zip", "cut.pdf"}
    assert found["zeros.png"].selected and found["cut.zip"].selected          # can't be opened at all
    assert not found["half.jpg"].selected and not found["cut.pdf"].selected   # still open in part
    assert "missing" in found["half.jpg"].detail


def test_broken_videos_and_music_are_found(tmp_path):
    from wick.cleanup import broken

    from .conftest import ffmpeg, have

    if not have("ffmpeg", "ffprobe"):
        pytest.skip("ffmpeg not installed")
    ffmpeg("-f", "lavfi", "-i", "testsrc=s=160x120:d=4", "-f", "lavfi", "-i", "sine=d=4", "-c:v", "libx264",
           "-c:a", "aac", "-shortest", "-movflags", "+faststart", str(tmp_path / "fine.mp4"))
    ffmpeg("-f", "lavfi", "-i", "sine=d=4", str(tmp_path / "fine.flac"))
    for name in ("fine.mp4", "fine.flac"):
        data = (tmp_path / name).read_bytes()
        (tmp_path / ("cut" + name[4:])).write_bytes(data[: len(data) * 6 // 10])
    (tmp_path / "noise.mp4").write_bytes(os.urandom(5000))
    found = {c.path.name: c for c in broken.find_broken([tmp_path]).items}
    assert set(found) == {"cut.mp4", "cut.flac", "noise.mp4"}
    assert found["noise.mp4"].selected and not found["cut.mp4"].selected


def test_the_same_song_is_found_in_other_formats(tmp_path):
    import numpy as np

    from wick.cleanup import duplicates, music

    from .conftest import ffmpeg, have

    if not have("ffmpeg", "ffprobe"):
        pytest.skip("ffmpeg not installed")

    def song(seed: int, path: Path) -> None:
        rng, rate = np.random.default_rng(seed), 22050
        out = []
        for _ in range(60):                                       # 30 s of random notes with overtones
            t = np.arange(rate // 2) / rate
            f = 110 * 2 ** (rng.integers(0, 36) / 12)
            out.append(sum(np.sin(2 * np.pi * f * h * t) / h for h in range(1, 5)) * np.exp(-t * rng.uniform(2, 8)))
        pcm = (np.concatenate(out) / 4 * 32767).astype("<i2")
        raw = path.with_suffix(".raw")
        raw.write_bytes(pcm.tobytes())
        ffmpeg("-f", "s16le", "-ar", str(rate), "-ac", "1", "-i", str(raw), "-metadata", f"title=Song {seed}",
               "-metadata", "artist=Band", str(path))
        raw.unlink()

    song(1, tmp_path / "best.flac")
    song(2, tmp_path / "other.flac")
    ffmpeg("-i", str(tmp_path / "best.flac"), "-b:a", "96k", "-af", "volume=-6dB", str(tmp_path / "copy.mp3"))
    ffmpeg("-i", str(tmp_path / "best.flac"), "-b:a", "64k", str(tmp_path / "small.m4a"))
    r = music.find_same_music([tmp_path], min_size=0)
    assert {c.path.name for c in r.items} == {"copy.mp3", "small.m4a"}
    assert all(c.keeper.name == "best.flac" for c in r.items)
    assert not any(c.selected for c in r.items)                   # lossy copies of a lossless song: you decide
    assert "FLAC, lossless" in r.items[0].detail
    swapped = duplicates.switch_keeper(r.items, tmp_path / "copy.mp3")
    assert {c.path.name for c in swapped} == {"best.flac", "small.m4a"}
    assert next(c for c in swapped if c.path.name == "best.flac").detail.endswith("is MP3, 96 kbps")
    tags = music.find_same_music([tmp_path], by="tags", min_size=0)
    assert {c.path.name for c in tags.items} == {"copy.mp3", "small.m4a"}


def test_the_same_video_is_found_in_other_sizes(tmp_path):
    from wick.cleanup import videos

    from .conftest import ffmpeg, have

    if not have("ffmpeg", "ffprobe"):
        pytest.skip("ffmpeg not installed")
    ffmpeg("-f", "lavfi", "-i", "testsrc2=s=640x360:d=6", "-f", "lavfi", "-i", "mandelbrot=s=640x360:r=25",
           "-filter_complex", "[0:v][1:v]blend=all_mode=addition,format=yuv420p", "-t", "6", "-c:v", "libx264",
           str(tmp_path / "best.mp4"))
    ffmpeg("-i", str(tmp_path / "best.mp4"), "-vf", "scale=320:180", "-c:v", "libx264", "-crf", "32",
           str(tmp_path / "small.mkv"))
    ffmpeg("-f", "lavfi", "-i", "mandelbrot=s=640x360:r=25", "-t", "6", "-c:v", "libx264",
           str(tmp_path / "other.mp4"))
    r = videos.find_similar_videos([tmp_path], min_size=0)
    assert [(c.path.name, c.keeper.name) for c in r.items] == [("small.mkv", "best.mp4")]
    assert r.items[0].selected and r.items[0].detail.startswith("180p, H.264")


def test_block_clone_on_refs():
    """ReFS (CI makes a small drive and sets WICK_REFS_DIR): a clone has the same contents, stays a separate
    file (changing it leaves the original alone), and the volume reports block cloning."""
    import os
    import pytest

    from wick import fileops

    root = os.environ.get("WICK_REFS_DIR")
    if os.name != "nt":
        assert not fileops.supports_block_clone(Path("."))
    if os.name != "nt" or not root:
        pytest.skip("needs a ReFS drive (WICK_REFS_DIR)")
    folder = Path(root) / "clone-test"
    folder.mkdir(exist_ok=True)
    a, b = folder / "a.bin", folder / "b.bin"
    for p in (a, b):
        if p.exists():
            p.unlink()
    data = os.urandom(3 * 1024 * 1024 + 12345)            # not a whole number of clusters
    a.write_bytes(data)
    assert fileops.supports_block_clone(a)
    fileops.clone_file(a, b)
    assert b.read_bytes() == data
    with open(b, "r+b") as f:
        f.write(b"changed")
    assert a.read_bytes() == data                          # independent: unlike a hard link
