"""Fast whole-drive scanning on Windows: read the NTFS file table (the MFT) directly, like WizTree.

Walking a drive folder by folder asks Windows about every file (minutes for a full drive); the MFT holds
every file's name, folder, size, date and attributes in one table that reads in seconds. Reading it needs
administrator rights (the raw volume), so the app asks Windows once (UAC) to run ``dump`` in a helper
process, which writes only what a scan needs: the folders under the chosen roots, and the files the app
can work on. If that isn't possible (declined, not NTFS, not Windows), the app scans the usual way.

The parser reads only the volume itself (boot sector, then the MFT's own runs), so it's tested on Linux
against real NTFS images (tests/data/ntfs.img.gz) as well as on a real C: drive on Windows CI.
"""

from __future__ import annotations

import gzip
import os
import struct
import sys
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

ROOT = 5                    # the MFT record of the root folder
_FILETIME_EPOCH = 116444736000000000

# $STANDARD_INFORMATION file attributes (the same values as Windows' FILE_ATTRIBUTE_*)
HIDDEN, SYSTEM, REPARSE = 0x2, 0x4, 0x400
CLOUD_ONLY = 0x1000 | 0x40000 | 0x400000      # OFFLINE | RECALL_ON_OPEN | RECALL_ON_DATA_ACCESS


class NotNTFS(Exception):
    pass


@dataclass
class Node:
    name: str = ""
    parent: int = -1
    is_dir: bool = False
    size: int = 0
    mtime: float = 0.0
    attrs: int = 0
    _name_rank: int = 9      # Win32 names beat POSIX names beat DOS 8.3 names


@dataclass
class Tree:
    nodes: dict[int, Node] = field(default_factory=dict)
    _children: dict[int, list[int]] | None = None

    def children(self, index: int) -> list[int]:
        if self._children is None:
            kids: dict[int, list[int]] = {}
            for i, n in self.nodes.items():
                if i != ROOT and n.parent in self.nodes:
                    kids.setdefault(n.parent, []).append(i)
            self._children = kids
        return self._children.get(index, [])

    def find(self, parts: list[str]) -> int | None:
        """The record of a path given as its parts below the drive root (case-insensitive, like Windows)."""
        cur = ROOT
        for part in parts:
            want = part.casefold()
            found = next((c for c in self.children(cur) if self.nodes[c].name.casefold() == want), None)
            if found is None:
                return None
            cur = found
        return cur


# ---------------------------------------------------------------- reading the volume

class Volume:
    """A raw NTFS volume (``\\\\.\\C:``) or an image file. Reads are sector-aligned (raw devices need that)."""

    def __init__(self, f):
        self.f = f
        boot = self._raw(0, 512)
        if boot[3:11] != b"NTFS    ":
            raise NotNTFS("Not an NTFS volume")
        self.sector = struct.unpack_from("<H", boot, 0x0B)[0]
        spc = boot[0x0D]
        spc = 1 << (256 - spc) if spc > 0x80 else spc
        self.cluster = self.sector * spc
        self.mft_lcn = struct.unpack_from("<Q", boot, 0x30)[0]
        cpr = struct.unpack_from("<b", boot, 0x40)[0]
        self.record_size = 1 << -cpr if cpr < 0 else cpr * self.cluster
        if not self.sector or not self.cluster or self.record_size < 512:
            raise NotNTFS("Unexpected NTFS geometry")

    def _raw(self, offset: int, size: int) -> bytes:
        sec = getattr(self, "sector", 512) or 512
        start = offset - offset % sec
        end = -(-(offset + size) // sec) * sec
        self.f.seek(start)
        data = self.f.read(end - start)
        return data[offset - start: offset - start + size]

    def read(self, offset: int, size: int) -> bytes:
        return self._raw(offset, size)


def _fixup(buf: bytearray, sector: int) -> bool:
    """Apply the update sequence array (NTFS stores each sector's last 2 bytes there). False if torn."""
    usa_off, usa_count = struct.unpack_from("<HH", buf, 4)
    if usa_off + usa_count * 2 > len(buf) or usa_count < 2:
        return False
    usn = buf[usa_off:usa_off + 2]
    for i in range(1, usa_count):
        end = i * sector - 2
        if end + 2 > len(buf):
            break
        if buf[end:end + 2] != usn:
            return False
        buf[end:end + 2] = buf[usa_off + 2 * i: usa_off + 2 * i + 2]
    return True


def runs(data: bytes, offset: int) -> list[tuple[int | None, int]]:
    """Decode a run list: [(first cluster or None for a sparse run, number of clusters)]."""
    out: list[tuple[int | None, int]] = []
    lcn = 0
    i = offset
    while i < len(data):
        head = data[i]
        if head == 0:
            break
        len_size, off_size = head & 0x0F, head >> 4
        i += 1
        length = int.from_bytes(data[i:i + len_size], "little")
        i += len_size
        if off_size:
            delta = int.from_bytes(data[i:i + off_size], "little", signed=True)
            lcn += delta
            out.append((lcn, length))
        else:
            out.append((None, length))
        i += off_size
    return out


@dataclass
class _Attr:
    type: int
    name: str
    resident: bool
    value: bytes = b""            # resident content
    start_vcn: int = 0
    real_size: int = 0
    run_list: list = field(default_factory=list)


def _attributes(rec: bytes) -> Iterator[_Attr]:
    off = struct.unpack_from("<H", rec, 0x14)[0]
    while off + 16 <= len(rec):
        atype, length = struct.unpack_from("<II", rec, off)
        if atype == 0xFFFFFFFF or length < 16 or off + length > len(rec):
            return
        nonres, name_len = rec[off + 8], rec[off + 9]
        name_off = struct.unpack_from("<H", rec, off + 10)[0]
        name = rec[off + name_off: off + name_off + 2 * name_len].decode("utf-16-le", "replace") if name_len else ""
        if not nonres:
            vlen, voff = struct.unpack_from("<IH", rec, off + 16)
            yield _Attr(atype, name, True, bytes(rec[off + voff: off + voff + vlen]))
        else:
            start_vcn = struct.unpack_from("<Q", rec, off + 16)[0]
            run_off = struct.unpack_from("<H", rec, off + 32)[0]
            real = struct.unpack_from("<Q", rec, off + 48)[0]
            yield _Attr(atype, name, False, b"", start_vcn, real, runs(rec[off:off + length], run_off))
        off += length


def _filetime(ft: int) -> float:
    return (ft - _FILETIME_EPOCH) / 1e7 if ft > _FILETIME_EPOCH else 0.0


def _record(vol: Volume, raw: bytes) -> bytes | None:
    buf = bytearray(raw)
    if buf[:4] != b"FILE" or not _fixup(buf, vol.sector):
        return None
    return bytes(buf)


def _mft_runs(vol: Volume) -> tuple[list[tuple[int | None, int]], int]:
    """The MFT's own clusters (it's a file too: record 0), and its size in bytes."""
    rec = _record(vol, vol.read(vol.mft_lcn * vol.cluster, vol.record_size))
    if rec is None:
        raise NotNTFS("The MFT's first record can't be read")
    pieces: list[tuple[int, list]] = []
    size = 0
    extension_refs: list[int] = []
    for a in _attributes(rec):
        if a.type == 0x80 and not a.name and not a.resident:
            pieces.append((a.start_vcn, a.run_list))
            if a.start_vcn == 0:
                size = a.real_size
        elif a.type == 0x20:            # $ATTRIBUTE_LIST: a very fragmented MFT continues elsewhere
            data = a.value
            i = 0
            while i + 26 <= len(data):
                etype, elen = struct.unpack_from("<IH", data, i)
                ref = struct.unpack_from("<Q", data, i + 16)[0] & 0xFFFFFFFFFFFF
                if etype == 0x80 and ref != 0:
                    extension_refs.append(ref)
                if elen == 0:
                    break
                i += elen
    if extension_refs:
        known = [r for _, rl in sorted(pieces) for r in rl]
        for ref in sorted(set(extension_refs)):
            raw = _read_record(vol, known, ref)
            ext = _record(vol, raw) if raw else None
            for a in _attributes(ext) if ext else ():
                if a.type == 0x80 and not a.name and not a.resident and a.start_vcn:
                    pieces.append((a.start_vcn, a.run_list))
    ordered = [r for _, rl in sorted(pieces, key=lambda p: p[0]) for r in rl]
    return ordered, size


def _read_record(vol: Volume, run_list, index: int) -> bytes | None:
    offset = index * vol.record_size
    for lcn, count in run_list:
        span = count * vol.cluster
        if offset < span:
            return None if lcn is None else vol.read(lcn * vol.cluster + offset, vol.record_size)
        offset -= span
    return None


def read_tree(f, progress: Callable[[int, int], None] | None = None, chunk_records: int = 4096) -> Tree:
    """Every file and folder in use on the volume ``f`` (a raw device or an image), with sizes, dates and
    attributes. ``progress(done, total)`` is called as records are read."""
    vol = Volume(f)
    run_list, mft_size = _mft_runs(vol)
    total = mft_size // vol.record_size if mft_size else 0
    tree = Tree()
    nodes = tree.nodes
    pending_sizes: dict[int, int] = {}      # sizes found in extension records, for their base record
    index = 0
    for lcn, count in run_list:
        span_records = count * vol.cluster // vol.record_size
        if lcn is None:
            index += span_records
            continue
        done_here = 0
        while done_here < span_records and (not total or index < total):
            n = min(chunk_records, span_records - done_here)
            data = vol.read(lcn * vol.cluster + done_here * vol.record_size, n * vol.record_size)
            for k in range(n):
                _parse(vol, data[k * vol.record_size:(k + 1) * vol.record_size], index + k, nodes, pending_sizes)
            index += n
            done_here += n
            if progress:
                progress(index, total)
    for base, size in pending_sizes.items():
        if base in nodes and not nodes[base].is_dir and not nodes[base].size:
            nodes[base].size = size
    return tree


def _parse(vol: Volume, raw: bytes, index: int, nodes: dict[int, Node], pending_sizes: dict[int, int]) -> None:
    if len(raw) < vol.record_size or raw[:4] != b"FILE":
        return
    rec = _record(vol, raw)
    if rec is None:
        return
    flags = struct.unpack_from("<H", rec, 0x16)[0]
    if not flags & 0x1:                   # not in use (a deleted file)
        return
    base = struct.unpack_from("<Q", rec, 0x20)[0] & 0xFFFFFFFFFFFF
    if base:                              # an extension record: only its file size matters here
        for a in _attributes(rec):
            if a.type == 0x80 and not a.name and not a.resident and a.start_vcn == 0:
                pending_sizes[base] = a.real_size
        return
    node = nodes.get(index) or Node(is_dir=bool(flags & 0x2))
    for a in _attributes(rec):
        if a.type == 0x10 and a.resident and len(a.value) >= 36:
            node.mtime = _filetime(struct.unpack_from("<Q", a.value, 8)[0])
            node.attrs = struct.unpack_from("<I", a.value, 32)[0]
        elif a.type == 0x30 and a.resident and len(a.value) >= 66:
            parent = struct.unpack_from("<Q", a.value, 0)[0] & 0xFFFFFFFFFFFF
            name_len, namespace = a.value[64], a.value[65]
            rank = {1: 0, 3: 0, 0: 1, 2: 2}.get(namespace, 3)
            if rank < node._name_rank:
                node.name = a.value[66:66 + 2 * name_len].decode("utf-16-le", "replace")
                node.parent = parent
                node._name_rank = rank
        elif a.type == 0x80 and not a.name:
            if a.resident:
                node.size = len(a.value)
            elif a.start_vcn == 0:
                node.size = a.real_size
    if node.name or index == ROOT:
        nodes[index] = node


# ---------------------------------------------------------------- what a scan needs, from a helper

def open_volume(drive: str):
    """``C:`` → the raw volume, opened for reading (needs administrator rights on Windows)."""
    return open(rf"\\.\{drive.rstrip(chr(92))}", "rb", buffering=0)


def drive_of(path: Path) -> str | None:
    drive = os.path.splitdrive(str(path))[0]
    return drive.upper() if len(drive) == 2 and drive[1] == ":" else None


def is_ntfs(path: Path) -> bool:
    if os.name != "nt":
        return False
    try:
        import ctypes

        fs = ctypes.create_unicode_buffer(32)
        root = (drive_of(path) or "") + "\\"
        ok = ctypes.windll.kernel32.GetVolumeInformationW(root, None, 0, None, None, None, fs, 32)
        return bool(ok) and fs.value.upper() == "NTFS"
    except Exception:  # noqa: BLE001
        return False


def dump(f, roots_parts: list[list[str]], keep: Callable[[str], bool], out: Path,
         progress: Callable[[int, int], None] | None = None) -> None:
    """Write (gzip) the folders under each root and the files ``keep(name)`` wants: what a scan needs.

    Lines: ``R\\t<record>\\t<root index>`` for each root found, ``D\\t<record>\\t<parent>\\t<attrs>\\t<marker>\\t<name>``
    for folders (marker: it holds a program library), ``F\\t<parent>\\t<size>\\t<mtime>\\t<attrs>\\t<name>``.
    """
    from .scanner import PROGRAM_MARKER_EXTS

    tree = read_tree(f, progress)
    with gzip.open(out, "wt", encoding="utf-8", newline="\n") as w:
        for i, parts in enumerate(roots_parts):
            top = tree.find(parts)
            if top is None:
                continue
            w.write(f"R\t{top}\t{i}\n")
            stack = [top]
            while stack:
                d = stack.pop()
                kids = tree.children(d)
                marker = any(not tree.nodes[c].is_dir
                             and os.path.splitext(tree.nodes[c].name)[1].lower() in PROGRAM_MARKER_EXTS for c in kids)
                n = tree.nodes[d]
                w.write(f"D\t{d}\t{n.parent}\t{n.attrs}\t{int(marker)}\t{_clean(n.name)}\n")
                for c in kids:
                    cn = tree.nodes[c]
                    if cn.is_dir:
                        stack.append(c)
                    elif keep(cn.name):
                        w.write(f"F\t{d}\t{cn.size}\t{cn.mtime:.3f}\t{cn.attrs}\t{_clean(cn.name)}\n")


def _clean(name: str) -> str:
    return name.replace("\t", " ").replace("\n", " ").replace("\r", " ")


@dataclass
class Listing:
    """A dump read back: folders and the files in them, for scanner.walk_listing."""

    roots: dict[int, int] = field(default_factory=dict)              # root index -> record
    dirs: dict[int, tuple[int, int, bool, str]] = field(default_factory=dict)    # record -> parent, attrs, marker, name
    subdirs: dict[int, list[int]] = field(default_factory=dict)
    files: dict[int, list[tuple[int, float, int, str]]] = field(default_factory=dict)  # folder -> files


def load(path: Path) -> Listing:
    out = Listing()
    with gzip.open(path, "rt", encoding="utf-8", newline="\n") as r:
        for line in r:
            parts = line.rstrip("\n").split("\t")
            if parts[0] == "R":
                out.roots[int(parts[2])] = int(parts[1])
            elif parts[0] == "D":
                rec, parent = int(parts[1]), int(parts[2])
                out.dirs[rec] = (parent, int(parts[3]), parts[4] == "1", parts[5])
                out.subdirs.setdefault(parent, []).append(rec)
            elif parts[0] == "F":
                out.files.setdefault(int(parts[1]), []).append(
                    (int(parts[2]), float(parts[3]), int(parts[4]), parts[5]))
    return out


# ---------------------------------------------------------------- asking for administrator rights

def helper_command(drive: str, out: Path, roots_parts: list[list[str]]) -> tuple[str, str]:
    """(program, arguments) that run ``dump`` for ``drive`` (the app itself when frozen)."""
    import subprocess

    args = ["--ntfs-dump", drive, str(out), *("\\".join(p) for p in roots_parts)]
    if getattr(sys, "frozen", False):
        return sys.executable, subprocess.list2cmdline(args)
    return sys.executable, subprocess.list2cmdline(["-m", "compressor.ntfs", *args])


def run_elevated(program: str, params: str, cancel=None) -> int | None:
    """Run ``program params`` as administrator (Windows asks the user), hidden; wait. Exit code, or None if
    the user said no (or it couldn't start)."""
    import ctypes
    import time
    from ctypes import wintypes

    class SHELLEXECUTEINFOW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("fMask", ctypes.c_ulong), ("hwnd", wintypes.HWND),
                    ("lpVerb", wintypes.LPCWSTR), ("lpFile", wintypes.LPCWSTR), ("lpParameters", wintypes.LPCWSTR),
                    ("lpDirectory", wintypes.LPCWSTR), ("nShow", ctypes.c_int), ("hInstApp", wintypes.HINSTANCE),
                    ("lpIDList", ctypes.c_void_p), ("lpClass", wintypes.LPCWSTR), ("hkeyClass", wintypes.HKEY),
                    ("dwHotKey", wintypes.DWORD), ("hIconOrMonitor", wintypes.HANDLE), ("hProcess", wintypes.HANDLE)]

    info = SHELLEXECUTEINFOW()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x00000040 | 0x00000400            # SEE_MASK_NOCLOSEPROCESS | SEE_MASK_FLAG_NO_UI
    info.lpVerb, info.lpFile, info.lpParameters, info.nShow = "runas", program, params, 0   # SW_HIDE
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)) or not info.hProcess:
        return None
    k32 = ctypes.windll.kernel32
    try:
        while k32.WaitForSingleObject(info.hProcess, 200) == 0x102:       # WAIT_TIMEOUT
            if cancel is not None and cancel.is_set():
                k32.TerminateProcess(info.hProcess, 1)
                time.sleep(0.2)
                return None
        code = wintypes.DWORD()
        k32.GetExitCodeProcess(info.hProcess, ctypes.byref(code))
        return code.value
    finally:
        k32.CloseHandle(info.hProcess)


def is_admin() -> bool:
    try:
        import ctypes

        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


def listing_for(roots: list[Path], keep: Callable[[str], bool], tmp_dir: Path, cancel=None) -> Listing | None:
    """The MFT listing for ``roots`` (all on one NTFS drive), asking for administrator rights if needed.
    None if it isn't possible: the caller then scans the usual way."""
    drive = drive_of(roots[0])
    if drive is None or any(drive_of(r) != drive for r in roots) or not is_ntfs(roots[0]):
        return None
    parts = [[p for p in Path(os.path.abspath(r)).parts[1:]] for r in roots]
    out = tmp_dir / f"compressor-mft-{os.getpid()}.tsv.gz"
    try:
        if is_admin():
            with open_volume(drive) as f:
                dump(f, parts, keep, out)
        else:
            program, params = helper_command(drive, out, parts)
            if run_elevated(program, params, cancel) != 0 or not out.exists():
                return None
        return load(out)
    except Exception:  # noqa: BLE001 - any problem: the usual scan instead
        from .log import log

        log.exception("Fast drive scanning wasn't possible; scanning the usual way")
        return None
    finally:
        try:
            out.unlink()
        except OSError:
            pass


def _main(argv: list[str]) -> int:
    """``--ntfs-dump C: out.tsv.gz root\\parts ...`` (run as administrator by listing_for)."""
    if len(argv) < 3 or argv[0] != "--ntfs-dump":
        print("usage: python -m compressor.ntfs --ntfs-dump C: OUT [ROOT ...]", file=sys.stderr)
        return 2
    from .formats import classify

    drive, out = argv[1], Path(argv[2])
    roots = [[p for p in r.split("\\") if p] for r in argv[3:]] or [[]]
    try:
        with open_volume(drive) as f:
            dump(f, roots, lambda n: classify(Path(n)) is not None or n.lower().endswith(".jxl"), out)
    except Exception as e:  # noqa: BLE001
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))


__all__ = ["read_tree", "Tree", "Node", "Volume", "NotNTFS", "runs", "dump", "load", "Listing", "listing_for",
           "drive_of", "is_ntfs", "ROOT"]
