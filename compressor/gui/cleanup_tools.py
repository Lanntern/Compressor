"""The Clean Up tools: what each one looks for, its options, and how its results are grouped."""

from __future__ import annotations

from typing import cast

import os
from pathlib import Path

from PySide6.QtWidgets import QComboBox, QFileDialog, QSpinBox

from ..cleanup import (
    actions, appcompress, archives, broken, caches, devclean, duplicates, empties, extensions, folders, installers,
    largefiles, music, similar, takeout, videos, winclean,
)
from ..cleanup.common import Candidate
from ..fileops import BIN_NAME
from .cleanup_section import Tool, ToolSection, _existing
from .widgets import button

# ---------------------------------------------------------------- tool definitions

def _keep_options(sec: ToolSection):
    keep = QComboBox()
    for label, value in (("Oldest copy", "oldest"), ("Newest copy", "newest"), ("Shortest path", "shortest_path"),
                         ("Copy in a chosen folder…", "folder")):
        keep.addItem(label, value)
    keep.setCurrentIndex(max(0, keep.findData(sec.qs.value("cleanup/duplicates/keep", "oldest", str))))
    keep.setToolTip("Which copy stays when files are identical")

    def changed():
        sec.qs.setValue("cleanup/duplicates/keep", keep.currentData())
        if keep.currentData() == "folder":
            d = QFileDialog.getExistingDirectory(sec, "Keep copies inside this folder", str(Path.home()))
            if d:
                sec.qs.setValue("cleanup/duplicates/prefer", d)
                keep.setToolTip(f"Keep the copy inside {d}")
            else:
                keep.setCurrentIndex(0)
    keep.currentIndexChanged.connect(changed)
    sec.extra_keep = keep
    minsize = QSpinBox()
    minsize.setRange(0, 10_000_000)
    minsize.setSuffix(" KB")
    minsize.setValue(cast(int, sec.qs.value("cleanup/duplicates/min_kb", 100, int)))
    minsize.valueChanged.connect(lambda v: sec.qs.setValue("cleanup/duplicates/min_kb", v))
    sec.extra_min = minsize
    return [("Keep", keep), ("Ignore files under", minsize)]


def _scan_duplicates(sec: ToolSection, progress, cancel):
    prefer = cast(str, sec.qs.value("cleanup/duplicates/prefer", "", str))
    return duplicates.find_duplicates(sec.folders, min_size=sec.extra_min.value() * 1024,
                                      keep=sec.extra_keep.currentData(), prefer=Path(prefer) if prefer else None,
                                      progress=progress, cancel=cancel)


def _similar_options(sec: "ToolSection"):
    how = QComboBox()
    for label, value in (("Nearly identical", "very"), ("Similar", "similar")):
        how.addItem(label, value)
    how.setCurrentIndex(max(0, how.findData(sec.qs.value("cleanup/similar/strictness", "very", str))))
    how.setToolTip("Nearly identical: re-saved, resized or slightly edited copies. Similar: also burst shots.")
    how.currentIndexChanged.connect(lambda: sec.qs.setValue("cleanup/similar/strictness", how.currentData()))
    sec.extra_how = how
    minsize = QSpinBox()
    minsize.setRange(0, 10_000_000)
    minsize.setSuffix(" KB")
    minsize.setValue(cast(int, sec.qs.value("cleanup/similar/min_kb", 50, int)))
    minsize.valueChanged.connect(lambda v: sec.qs.setValue("cleanup/similar/min_kb", v))
    sec.extra_min = minsize
    return [("Match", how), ("Ignore photos under", minsize)]


def _large_options(sec: "ToolSection"):
    minsize = QSpinBox()
    minsize.setRange(10, 1_000_000)
    minsize.setSingleStep(100)
    minsize.setSuffix(" MB")
    minsize.setValue(cast(int, sec.qs.value("cleanup/large/min_mb", 500, int)))
    minsize.valueChanged.connect(lambda v: sec.qs.setValue("cleanup/large/min_mb", v))
    minsize.setAccessibleName("Larger than, in megabytes")
    sec.extra_min = minsize
    kind = QComboBox()
    for value, label in largefiles.KINDS.items():
        kind.addItem(label, value)
    kind.setCurrentIndex(max(0, kind.findData(sec.qs.value("cleanup/large/kind", "all", str))))
    kind.currentIndexChanged.connect(lambda: sec.qs.setValue("cleanup/large/kind", kind.currentData()))
    kind.setAccessibleName("Type of file")
    sec.extra_how = kind
    return [("Larger than", minsize), ("Type", kind)]


def _scan_large(sec: "ToolSection", progress, cancel):
    return largefiles.find_large_files(sec.folders, sec.extra_min.value(), sec.extra_how.currentData(), progress,
                                       cancel)


def _scan_similar(sec: "ToolSection", progress, cancel):
    return similar.find_similar(sec.folders, strictness=sec.extra_how.currentData(),
                                min_size=sec.extra_min.value() * 1024, progress=progress, cancel=cancel)


def _videos_options(sec: "ToolSection"):
    how = QComboBox()
    for label, value in (("Nearly identical", "very"), ("Similar", "similar")):
        how.addItem(label, value)
    how.setCurrentIndex(max(0, how.findData(sec.qs.value("cleanup/videos/strictness", "very", str))))
    how.setToolTip("Nearly identical: the same video in another size or format. Similar: also copies with "
                   "changed colours, a watermark or subtitles burned in.")
    how.currentIndexChanged.connect(lambda: sec.qs.setValue("cleanup/videos/strictness", how.currentData()))
    sec.extra_how = how
    return [("Match", how)]


def _music_options(sec: "ToolSection"):
    how = QComboBox()
    for label, value in (("Same recording", "sound"), ("Same title and artist", "tags")):
        how.addItem(label, value)
    how.setCurrentIndex(max(0, how.findData(sec.qs.value("cleanup/music/by", "sound", str))))
    how.setToolTip("Same recording: listens to the songs, whatever their names and tags. Same title and artist: "
                   "only reads the tags (faster).")
    how.currentIndexChanged.connect(lambda: sec.qs.setValue("cleanup/music/by", how.currentData()))
    sec.extra_how = how
    return [("Match", how)]


def _scan_music(sec: "ToolSection", progress, cancel):
    phone = cast(str, sec.qs.value("mirror/dest", "", str))      # (the phone copy is meant to repeat the library)
    return music.find_same_music(sec.folders, by=sec.extra_how.currentData(), exclude=[Path(phone)] if phone else [],
                                 progress=progress, cancel=cancel)


def _broken_options(sec: "ToolSection"):
    from PySide6.QtWidgets import QCheckBox

    box = QCheckBox("Play videos and music all the way through")
    box.setToolTip("Finds damage anywhere in a video or song, not just a missing end. Takes about as long as "
                   "converting them.")
    box.setChecked(cast(bool, sec.qs.value("cleanup/broken/thorough", False, bool)))
    box.toggled.connect(lambda on: sec.qs.setValue("cleanup/broken/thorough", on))
    sec.extra_thorough = box
    return [("", box)]


def _similar_group_title(g: str, items: list[Candidate]) -> str:
    return f"{len(items) + 1} similar photos: keeping {items[0].keeper.name}"


def _days_option(key: str, default: int, label: str, tip: str):
    def make(sec: ToolSection):
        spin = QSpinBox()
        spin.setRange(1, 3650)
        spin.setSuffix(" days")
        spin.setValue(cast(int, sec.qs.value(f"cleanup/{key}/days", default, int)))
        spin.setToolTip(tip)
        spin.valueChanged.connect(lambda v: sec.qs.setValue(f"cleanup/{key}/days", v))
        sec.extra_days = spin
        return [(label, spin)]
    return make


def _appcompress_options(sec: ToolSection):
    """Keep compressed games compressed: updates add uncompressed files, so the app compresses those again
    a few minutes after it starts."""
    from PySide6.QtWidgets import QCheckBox

    box = QCheckBox("Compress again after game updates")
    box.setToolTip("Games you compressed here get new, uncompressed files when they update. A few minutes after "
                   "Compressor starts it compresses those again, in the background, skipping games that are "
                   "running.")
    box.setChecked(cast(bool, sec.qs.value("appcompress/recompress", True, bool)))
    box.toggled.connect(lambda on: sec.qs.setValue("appcompress/recompress", on))
    return [("", box)]


def _takeout_options(sec: ToolSection):
    albums = QComboBox()
    for label, value in (("As links (no extra space)", "link"), ("As copies", "copy"), ("Leave out", "skip")):
        albums.addItem(label, value)
    albums.setToolTip("Takeout repeats every album photo in the year folders. Links keep your albums as folders "
                      "without storing the photos twice (they're hard links: the same file in two places).")
    albums.setCurrentIndex(max(0, albums.findData(sec.qs.value("cleanup/takeout/albums", "link", str))))
    albums.currentIndexChanged.connect(lambda: sec.qs.setValue("cleanup/takeout/albums", albums.currentData()))
    edited = QComboBox()
    for label, value in (("Keep both", "both"), ("Only the edited one", "edited"), ("Only the original", "original")):
        edited.addItem(label, value)
    edited.setToolTip("Photos you edited in Google Photos come out twice: the original and \"…-edited\".")
    edited.setCurrentIndex(max(0, edited.findData(sec.qs.value("cleanup/takeout/edited", "both", str))))
    edited.currentIndexChanged.connect(lambda: sec.qs.setValue("cleanup/takeout/edited", edited.currentData()))
    sec.takeout_albums, sec.takeout_edited = albums, edited
    return [("Albums", albums), ("Edited photos", edited)]


def _scan_caches(sec: ToolSection, progress, cancel):
    return caches.find_caches(progress, cancel)


def _protected(sec: ToolSection) -> list[str]:
    return [str(x) for x in (sec.qs.value("cleanup/devclean/protected", [], list) or []) if x]


def _set_protected(sec: ToolSection, ids: list[str]) -> None:
    sec.qs.setValue("cleanup/devclean/protected", sorted(set(ids)))
    _refresh_protected(sec)


def _protected_name(pid: str) -> str:
    kind, _, value = pid.partition(":")
    if kind == "remote":
        return value.rstrip("/").rsplit("/", 1)[-1].rsplit(":", 1)[-1] + "  (git remote)"
    return Path(value).name or value


def _refresh_protected(sec: ToolSection) -> None:
    btn = getattr(sec, "protected_btn", None)
    if btn is not None:
        n = len(_protected(sec))
        btn.setText(f"Protected ({n})" if n else "Protected")


def _dev_off(sec: ToolSection, what: str) -> list[str]:
    """Switched-off kinds ("kinds") or folder names ("names")."""
    return [str(x) for x in (sec.qs.value(f"cleanup/devclean/off_{what}", [], list) or []) if x]


def _set_dev_off(sec: ToolSection, what: str, values: list[str]) -> None:
    sec.qs.setValue(f"cleanup/devclean/off_{what}", sorted(set(values)))
    _refresh_kinds(sec)


def _refresh_kinds(sec: ToolSection) -> None:
    btn = getattr(sec, "kinds_btn", None)
    if btn is not None:
        off = len(_dev_off(sec, "kinds")) + len(_dev_off(sec, "names"))
        btn.setText(f"Show ({off} off)" if off else "Show")


def _kinds_button(sec: ToolSection):
    """Switch whole kinds of folder on or off (every node_modules, every virtual environment...), plus the
    single folder names switched off from the context menu."""
    from PySide6.QtWidgets import QMenu

    btn = button("Show", "", "eye", "Choose which kinds of folders and caches to look for")
    menu = QMenu(btn)

    def toggle(kind: str, on: bool) -> None:
        off = [k for k in _dev_off(sec, "kinds") if k != kind]
        _set_dev_off(sec, "kinds", off if on else off + [kind])

    def fill():
        menu.clear()
        off = set(_dev_off(sec, "kinds"))
        for kind, label in devclean.KINDS.items():
            a = menu.addAction(label)
            a.setCheckable(True)
            a.setChecked(kind not in off)
            a.toggled.connect(lambda on, k=kind: toggle(k, on))
        names = _dev_off(sec, "names")
        if names:
            menu.addSeparator()
            for name in names:
                menu.addAction(f"Suggest {name} Folders Again",
                               lambda n=name: _set_dev_off(sec, "names", [x for x in _dev_off(sec, "names")
                                                                          if x != n]))
        menu.addSeparator()
        a = menu.addAction("Changes apply to the next scan.")
        a.setEnabled(False)

    menu.aboutToShow.connect(fill)
    btn.setMenu(menu)
    sec.kinds_btn = btn
    _refresh_kinds(sec)
    return btn


def _dev_options(sec: ToolSection):
    from PySide6.QtWidgets import QMenu

    opts = _days_option("devclean", 30, "Pre-select projects untouched for",
                        "Projects with changed files or git activity more recently are listed but not "
                        "pre-selected")(sec)
    btn = button("Protected", "", "lock", "Projects and folders that are never listed. Right-click a result to "
                                          "protect it.")
    menu = QMenu(btn)

    def fill():
        menu.clear()
        ids = _protected(sec)
        if not ids:
            a = menu.addAction("Nothing is protected yet. Right-click a result to protect its project.")
            a.setEnabled(False)
            return
        for pid in ids:
            menu.addAction(f"Stop Protecting {_protected_name(pid)}",
                           lambda p=pid: _set_protected(sec, [x for x in _protected(sec) if x != p]))

    menu.aboutToShow.connect(fill)
    btn.setMenu(menu)
    sec.protected_btn = btn
    _refresh_protected(sec)
    return opts + [("", btn), ("", _kinds_button(sec))]


def _dev_menu(sec: ToolSection, items: list[Candidate]):
    """Right-click: never clean this project (remembered by its git remote, so moving it keeps it
    protected) or this one folder."""
    projects = {c.extra["project_id"]: Path(c.extra["project"]).name for c in items if c.extra.get("project_id")}
    entries = []

    def protect(ids: list[str]) -> None:
        _set_protected(sec, _protected(sec) + ids)
        if sec.result is not None:
            prot = set(_protected(sec))
            sec._remember_checks()
            sec.result.items = [c for c in sec.result.items if c.extra.get("project_id") not in prot
                                and "path:" + os.path.normcase(str(c.path)) not in prot]
            sec._fill(sec.result)

    if len(projects) == 1:
        (pid, name), = projects.items()
        entries.append((f"Never Clean {name}", lambda: protect([pid])))
    elif projects:
        entries.append((f"Never Clean These {len(projects)} Projects", lambda: protect(list(projects))))
    names = sorted({c.path.name for c in items if not c.extra.get("docker") and c.extra.get("project")})
    if len(names) == 1:
        def hide_name(name=names[0]) -> None:
            _set_dev_off(sec, "names", _dev_off(sec, "names") + [name])
            if sec.result is not None:
                sec._remember_checks()
                sec.result.items = [c for c in sec.result.items if c.path.name != name or not c.extra.get("project")]
                sec._fill(sec.result)

        entries.append((f"Never Suggest {names[0]} Folders", hide_name))
    if len(items) == 1 and not items[0].extra.get("docker"):
        c = items[0]
        entries.append(("Never Clean This Folder", lambda: protect(["path:" + os.path.normcase(str(c.path))])))
    return entries


def _scan_dev(sec: ToolSection, progress, cancel):
    return devclean.find_dev_clutter(sec.folders, sec.extra_days.value(), protected=set(_protected(sec)),
                                     kinds_off=set(_dev_off(sec, "kinds")), names_off=set(_dev_off(sec, "names")),
                                     progress=progress, cancel=cancel)


def _dup_group_title(g: str, items: list[Candidate]) -> str:
    return f"{len(items) + 1} copies of {items[0].keeper.name}"


def tools() -> list[Tool]:
    return [
        Tool("duplicates", "Duplicates",
             "Finds files that are byte-for-byte identical (checked with a full checksum, not just the name). "
             f"Keep one copy and move the others to the {BIN_NAME}, or replace them with hard links so every "
             "path keeps working while the contents are stored once.",
             lambda: _existing("Pictures", "Videos", "Documents", "Downloads", "Desktop", "Music"),
             _scan_duplicates,
             [(actions.HARDLINK, "Replace with Hard Links", ""), (actions.TRASH, f"Move to {BIN_NAME}", "primary")],
             options=_keep_options, group_title=_dup_group_title),
        Tool("similar", "Similar Photos",
             "Finds photos that look alike even when the files differ: re-saved, resized or edited copies, and "
             "(on Similar) burst shots. The best one of each group stays (most pixels, then sharpest). Only "
             "obvious copies are pre-selected; hover a row to see the photo.",
             lambda: _existing("Pictures", "Desktop", "Downloads"), _scan_similar,
             [(actions.TRASH, f"Move to {BIN_NAME}", "primary")],
             options=_similar_options, group_title=_similar_group_title),
        Tool("videos", "Similar Videos",
             "Finds the same video saved more than once as different files: a smaller copy, another resolution "
             "or format, a second download. The copy with the most pixels stays; lower-resolution copies that "
             "look the same are pre-selected.",
             lambda: _existing("Videos", "Pictures", "Downloads", "Desktop"),
             lambda s, p, c: videos.find_similar_videos(s.folders, strictness=s.extra_how.currentData(),
                                                        progress=p, cancel=c),
             [(actions.TRASH, f"Move to {BIN_NAME}", "primary")], options=_videos_options,
             group_title=videos.group_title),
        Tool("music", "Same Music",
             "Finds songs saved more than once, even as different files: an MP3 and a FLAC of the same "
             "recording, or two downloads at different bitrates. The best copy stays (lossless, then the "
             "highest bitrate). Only copies in the same format as the one that stays are pre-selected.",
             lambda: _existing("Music", "Downloads", "Desktop", "Documents"), _scan_music,
             [(actions.TRASH, f"Move to {BIN_NAME}", "primary")], options=_music_options,
             group_title=music.group_title),
        Tool("large", "Large Files",
             "The biggest files in these folders, largest first: old videos, disk images, downloads you forgot. "
             "Nothing is pre-selected.",
             lambda: [Path.home()], _scan_large, [(actions.TRASH, f"Move to {BIN_NAME}", "primary")],
             options=_large_options),
        Tool("folders", "Folder Sizes",
             "How much space each folder takes, biggest first. Open a folder to see what's inside it, all the way "
             "down. Hidden and program folders are measured too; folders apps and Windows need can't be selected.",
             lambda: [Path.home()], lambda s, p, c: folders.find_folder_sizes(s.folders, p, c),
             [(actions.TRASH, f"Move to {BIN_NAME}", "primary")]),
        Tool("extensions", "Wrong Extensions",
             "Files whose contents are another type than their name says, like a PNG called .jpg or an iPhone "
             "photo (HEIC) called .jpg. Some apps refuse to open them. Renaming fixes that, and can be undone.",
             lambda: [Path.home()], lambda s, p, c: extensions.find_wrong_extensions(s.folders, p, c),
             [(actions.RENAME, "Fix Names", "primary")]),
        Tool("broken", "Broken Files",
             "Photos, videos, music, archives and PDFs that can't be opened, or whose end is missing after an "
             "interrupted download or copy. Files that can't be opened at all are pre-selected; damaged ones "
             "often still open in part, so you decide.",
             lambda: _existing("Pictures", "Videos", "Music", "Documents", "Downloads", "Desktop"),
             lambda s, p, c: broken.find_broken(s.folders, s.extra_thorough.isChecked(), p, c),
             [(actions.TRASH, f"Move to {BIN_NAME}", "primary")], options=_broken_options),
        Tool("archives", "Archives",
             "Finds .zip, .7z and .tar archives that have already been extracted next to themselves. An archive is "
             "only suggested when every file in it is present and identical in the extracted folder.",
             lambda: _existing("Downloads", "Desktop", "Documents"), lambda s, p, c: archives.find_extracted_archives(
                 s.folders, progress=p, cancel=c),
             [(actions.TRASH, f"Move to {BIN_NAME}", "primary")]),
        Tool("installers", "Installers",
             "Installers and disk images you downloaded a while ago. Installers for programs that look installed "
             "are marked; disk images are never pre-selected.",
             lambda: _existing("Downloads"),
             lambda s, p, c: installers.find_installers(s.folders, s.extra_days.value(), p, c),
             [(actions.TRASH, f"Move to {BIN_NAME}", "primary")],
             options=_days_option("installers", 30, "Older than", "Only list files older than this")),
        Tool("empty", "Empty Items",
             "Empty folders and zero-byte files. Files that mean something even when empty (like __init__.py "
             "or .gitkeep) are left alone, and zero-byte files are never pre-selected.",
             lambda: _existing("Documents", "Downloads", "Desktop", "Pictures"),
             lambda s, p, c: empties.find_empty(s.folders, p, c),
             [(actions.TRASH, f"Move to {BIN_NAME}", "primary")]),
        Tool("caches", "Caches",
             "Caches that apps rebuild by themselves: browsers, Discord, Spotify, shader caches, Lightroom "
             "previews and old catalog backups, Adobe's media cache, Telegram, Slack and Teams, and leftover "
             "driver installers. Developer caches are on the Developer tab.",
             None, _scan_caches, [(actions.TRASH, f"Move to {BIN_NAME}", "primary")]),
        Tool("devclean", "Developer",
             "Folders in your code projects that tools rebuild, such as node_modules, build output, Pods, "
             "virtual environments and Unity's Library, plus package and IDE caches like DerivedData, npm, Gradle "
             "and pip. Source code, settings and anything committed to git are never touched.",
             lambda: [Path.home()], _scan_dev, [(actions.TRASH, f"Move to {BIN_NAME}", "primary")],
             folder_label="Projects in",
             options=_dev_options, menu=_dev_menu,
             note="A folder is only listed when the file that recreates it is next to it (package.json beside "
                  f"node_modules, Podfile beside Pods...). Everything goes to the {BIN_NAME} and can be "
                  "restored later.",
             group_title=devclean.group_title),
        Tool("winclean", "Windows",
             "Leftovers Windows itself makes: the previous Windows installation after a big update, Windows Update "
             "leftovers, Delivery Optimization's download cache, error reports, memory dumps and upgrade logs, plus "
             "your old temporary files.",
             None, lambda s, p, c: winclean.find(p, c), [(actions.TRASH, "Clean Up", "primary")],
             note="System items are removed with Windows' own tools (the same ones Disk Cleanup uses), never "
                  f"deleted file by file. Your own files go to the {BIN_NAME}.",
             available=lambda: "" if winclean.supported() else "Available on Windows only."),
        Tool("appcompress", "Compress Apps",
             "Compresses installed games and programs with Windows' built-in LZX compression. Files stay where "
             "they are and keep working; Windows decompresses them on the fly. Most games shrink 30–60%.",
             appcompress.default_folders,
             lambda s, p, c: appcompress.find_compressible(s.folders, p, c),
             [(actions.COMPACT, "Compress Folders", "primary")],
             note="Games that use DirectStorage are listed but not suggested (compression would slow their "
                  "loading). Don't compress a game while it's running.",
             options=_appcompress_options,
             available=lambda: "" if appcompress.supported() else "Available on Windows (NTFS drives) only."),
        Tool("takeout", "Google Takeout",
             "Turns a Google Photos Takeout (many takeout-….zip or .tgz files) into one folder of photos and videos "
             "with the dates they were taken: Google keeps those in separate JSON files and gives every photo the "
             "export date. Album copies don't take extra space, and running it again picks up where it stopped.",
             lambda: _existing("Downloads"), lambda s, p, c: takeout.find(s.folders, p, c),
             [(actions.TAKEOUT, "Merge Into Folder…", "primary")], folder_label="Takeout files in",
             options=_takeout_options),
    ]
