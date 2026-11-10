"""Wick as an MCP server (``wick mcp FOLDER...``): lets an AI agent find savings, compress files and
undo past runs, over the Model Context Protocol's stdio transport (newline-delimited JSON-RPC 2.0).

Built so that an agent that misunderstands, or is misled by text it read somewhere, can't lose anyone's files:

* **Only the folders the user chose.** Every path is fully resolved (symlinks and junctions too) and must lie
  inside one of them; system and program locations are refused even inside a chosen folder.
* **Plan, then a person says yes.** Planning is read-only and returns a plan ID; compressing takes only that
  ID, never paths. Before anything changes, Wick itself asks the person in a Windows dialog (elsewhere:
  through the client's own confirmation request), which the agent can neither see nor answer. No answer, no
  change. Undo asks the same way.
* **Nothing is ever deleted permanently.** Originals go to the Recycle Bin (never straight to deletion, and
  never when the Recycle Bin is too small or missing), every run is recorded in History, and any run can be
  undone. With History turned off in the app, originals are always kept.
* **Bounded.** Plans expire and are single-use, files that changed since planning are left alone, runs are
  capped in size, calls are rate-limited, one job runs at a time.
* **Quiet.** Nothing about the conversation is stored; the log notes which tool ran, not its arguments.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import APP_NAME, __version__
from .fileops import BIN_NAME

PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
PLAN_LIFETIME = 30 * 60                 # seconds a plan can be carried out
CONFIRM_TIMEOUT = 10 * 60               # seconds a person has to answer before the request is declined
DEFAULT_MAX_FILES = 1000
DEFAULT_PRESET = "Photo archive"        # lossless and reversible for photos: the safest choice
RATE_LIMITS = {                         # tool -> (calls, per seconds)
    "list_allowed_folders": (60, 60), "list_history": (60, 60), "plan_compression": (30, 60),
    "compress_planned_files": (10, 3600), "undo_run": (10, 3600),
}

# Unicode that can hide or reorder text (bidi overrides, zero-width characters) and control characters: never
# passed on from file names, which anyone can choose (they could be written to look like instructions).
_UNSAFE = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


def clean(text, limit: int = 260) -> str:
    """Text from the file system made safe to show an agent: no control or hidden characters, bounded."""
    s = _UNSAFE.sub("\uFFFD", str(text))
    return s if len(s) <= limit else s[: limit - 1] + "…"


def _norm(p: Path) -> str:
    return os.path.normcase(str(p)).rstrip("\\/")


def _resolve(p: str | Path) -> Path:
    """The real location: ~ expanded, made absolute, symlinks and junctions followed."""
    return Path(os.path.realpath(os.path.abspath(os.path.expanduser(str(p)))))


def _inside(path: Path, root: Path) -> bool:
    a, b = _norm(path), _norm(root)
    return a == b or a.startswith(b + os.sep)


def forbidden_roots() -> list[Path]:
    """Places an agent never touches, even inside an allowed folder: the system, installed programs, apps'
    own data, and this app's History (which is what makes undo possible)."""
    from .journal import data_dir

    out = [data_dir()]
    if os.name == "nt":
        env = os.environ
        for key in ("SystemRoot", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "ProgramData", "APPDATA",
                    "LOCALAPPDATA"):
            if env.get(key):
                out.append(Path(env[key]))
        drive = env.get("SystemDrive", "C:")
        out += [Path(drive + "\\$Recycle.Bin"), Path(drive + "\\System Volume Information"),
                Path(drive + "\\Recovery"), Path(drive + "\\Boot")]
    else:
        out += [Path(p) for p in ("/bin", "/boot", "/dev", "/etc", "/lib", "/lib64", "/opt", "/proc", "/root", "/run",
                                  "/sbin", "/snap", "/sys", "/usr", "/var", "/Applications", "/Library", "/System")]
        out += [Path.home() / ".local", Path.home() / ".config", Path.home() / ".cache",
                Path.home() / "Library"]
    return [_resolve(p) for p in out]


class ToolError(Exception):
    """A problem the agent can act on (reported as a tool result with isError, per the spec)."""


@dataclass
class _Plan:
    plan_id: str
    created: float
    settings: Any
    root: Path
    items: list                      # [(FileEntry, Plan)] to convert
    stamps: dict = field(default_factory=dict)    # path -> (size, mtime) when planned
    preset: str = ""


class Server:
    def __init__(self, folders: list[str], keep_originals: bool = False, max_files: int = DEFAULT_MAX_FILES,
                 confirm=None, out=None, forbidden: list[Path] | None = None):
        self.keep_originals = keep_originals
        self.max_files = max(1, min(int(max_files), 100_000))
        self.forbidden = forbidden_roots() if forbidden is None else [_resolve(p) for p in forbidden]
        self.allowed: list[Path] = []
        self.refused: list[tuple[str, str]] = []
        for f in folders:
            if not str(f).strip():
                continue
            p = _resolve(f)
            why = ("not a folder" if not p.is_dir() else
                   "a system, program or app-data location" if self._forbidden(p) else "")
            if why:
                self.refused.append((clean(f), why))
            else:
                self.allowed.append(p)
        self._confirm = confirm                 # tests: a function(text) -> bool
        self._out = out
        self._write_lock = threading.Lock()
        self._job_lock = threading.Lock()
        self._plans: dict[str, _Plan] = {}
        self._plans_lock = threading.Lock()
        self._calls: dict[str, deque] = {}
        self._cancels: dict = {}                # request id -> threading.Event
        self._pending: dict = {}                # our requests to the client: id -> [Event, response]
        self._next_id = 0
        self._threads: list[threading.Thread] = []
        self.client_caps: dict = {}

    # ---------------------------------------------------------------- paths
    def _forbidden(self, p: Path) -> bool:
        return any(_inside(p, f) for f in self.forbidden)

    def _check_path(self, raw: str) -> Path:
        if not isinstance(raw, str) or not raw.strip():
            raise ToolError("Each path must be a non-empty string.")
        p = _resolve(raw)
        if not any(_inside(p, a) for a in self.allowed):
            raise ToolError(f"{clean(raw)} is outside the folders Wick may use. Allowed: "
                            + "; ".join(clean(a) for a in self.allowed)
                            + ". The user can add folders in the extension's settings.")
        if self._forbidden(p):
            raise ToolError(f"{clean(raw)} is a system, program or app-data location, which Wick never "
                            "changes for an agent.")
        if not p.exists():
            raise ToolError(f"{clean(raw)} doesn't exist.")
        return p

    def _usable(self, p: Path) -> bool:
        r = _resolve(p)
        return any(_inside(r, a) for a in self.allowed) and not self._forbidden(r)

    # ---------------------------------------------------------------- settings
    def _history_on(self) -> bool:
        try:
            from PySide6.QtCore import QSettings

            path = os.environ.get("WICK_SETTINGS_FILE")
            qs = QSettings(path, QSettings.IniFormat) if path else QSettings(APP_NAME, APP_NAME)
            return bool(qs.value("history_enabled", True, bool))
        except Exception:  # noqa: BLE001 - assume the safer answer
            return False

    def replaces_originals(self) -> bool:
        return not self.keep_originals and self._history_on()

    def _settings(self, preset: str):
        from . import presets
        from .settings import Settings

        s = presets.apply(preset, Settings())
        s.output_mode = "replace" if self.replaces_originals() else "suffix"
        s.output_suffix = "_compressed"
        s.allow_permanent_delete = False        # never: originals go to the Recycle Bin or stay where they are
        s.skip_program_folders = True
        s.include_hidden = False
        s.low_priority = True
        s.validate()
        return s

    # ---------------------------------------------------------------- tools
    def tools(self) -> list[dict]:
        from . import presets

        names = presets.names()
        originals = (f"Originals are moved to the {BIN_NAME} (never deleted permanently) and the run can be undone "
                     "with undo_run." if self.replaces_originals() else
                     "Originals are kept; compressed copies are saved next to them with \"_compressed\" in the name.")
        return [
            {"name": "list_allowed_folders", "title": "List allowed folders",
             "description": "Lists the folders Wick may work in for you, what happens to original files, and "
                            "the presets you can choose. Call this first if you don't know which folders are allowed.",
             "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
             "annotations": {"title": "List allowed folders", "readOnlyHint": True, "destructiveHint": False,
                             "idempotentHint": True, "openWorldHint": False}},
            {"name": "plan_compression", "title": "Plan compression",
             "description": "Scans folders or files (inside the allowed folders) for photos, videos, music and "
                            "documents (the pictures inside Office files and PDFs) that can be stored smaller, and "
                            "estimates the savings. Changes nothing. Returns a plan_id to "
                            "pass to compress_planned_files if the user wants to go ahead. Use it when the user asks "
                            "how much space could be saved or to shrink media files.",
             "inputSchema": {"type": "object", "additionalProperties": False, "required": ["paths"], "properties": {
                 "paths": {"type": "array", "minItems": 1, "maxItems": 20, "items": {"type": "string"},
                           "description": "Folders and/or files to look at, e.g. [\"D:\\\\Photos\\\\2023\"]."},
                 "preset": {"type": "string", "enum": names, "default": DEFAULT_PRESET,
                            "description": "How to compress. " + " ".join(
                                f"{n}: {presets.description(n)}" for n in names if presets.is_builtin(n))},
                 "list_files": {"type": "integer", "minimum": 0, "maximum": 100, "default": 10,
                                "description": "How many of the largest files to list (0 for totals only)."}}},
             "annotations": {"title": "Plan compression", "readOnlyHint": True, "destructiveHint": False,
                             "idempotentHint": True, "openWorldHint": False}},
            {"name": "compress_planned_files", "title": "Compress planned files",
             "description": "Compresses exactly the files in a plan from plan_compression, after the user approves "
                            "it in a confirmation that Wick shows them (if they decline or don't answer, "
                            "nothing changes). " + originals + " Only use it when the user has asked to compress.",
             "inputSchema": {"type": "object", "additionalProperties": False, "required": ["plan_id"], "properties": {
                 "plan_id": {"type": "string", "description": "The plan_id returned by plan_compression."}}},
             "annotations": {"title": "Compress planned files", "readOnlyHint": False, "destructiveHint": True,
                             "idempotentHint": False, "openWorldHint": False}},
            {"name": "list_history", "title": "List past runs",
             "description": "Lists recent compression runs in the allowed folders (newest first), with their run_id, "
                            "date, file count and space saved. Changes nothing.",
             "inputSchema": {"type": "object", "additionalProperties": False, "properties": {
                 "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 5,
                           "description": "How many runs to list."}}},
             "annotations": {"title": "List past runs", "readOnlyHint": True, "destructiveHint": False,
                             "idempotentHint": True, "openWorldHint": False}},
            {"name": "undo_run", "title": "Undo a run",
             "description": "Restores the original files of a past run (from list_history) and removes its "
                            "compressed versions, after the user approves it in a confirmation that Wick "
                            "shows them. Only use it when the user asks to undo or restore.",
             "inputSchema": {"type": "object", "additionalProperties": False, "required": ["run_id"], "properties": {
                 "run_id": {"type": "string", "description": "A run_id from list_history."}}},
             "annotations": {"title": "Undo a run", "readOnlyHint": False, "destructiveHint": True,
                             "idempotentHint": False, "openWorldHint": False}},
        ]

    def call_tool(self, name: str, args: dict, request_id=None, progress_token=None) -> dict:
        from .log import log

        handlers = {"list_allowed_folders": self.t_list_allowed_folders, "plan_compression": self.t_plan,
                    "compress_planned_files": self.t_compress, "list_history": self.t_history,
                    "undo_run": self.t_undo}
        if name not in handlers:
            raise KeyError(name)
        if not isinstance(args, dict):
            return self._error("Arguments must be an object.")
        schema = next(t["inputSchema"] for t in self.tools() if t["name"] == name)
        unknown = sorted(set(args) - set(schema.get("properties", {})))
        missing = [k for k in schema.get("required", []) if k not in args]
        if unknown or missing:
            return self._error("Invalid arguments: " + "; ".join(
                ([f"unexpected {', '.join(clean(k, 40) for k in unknown)}"] if unknown else [])
                + ([f"missing {', '.join(missing)}"] if missing else []))
                + f". Expected: {', '.join(schema.get('properties', {})) or 'no arguments'}.")
        log.info("MCP: %s", name)          # (which tool ran: never its arguments or anything from the conversation)
        try:
            self._rate_limit(name)
            result = handlers[name](args, request_id=request_id, progress_token=progress_token)
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False,
                                                                    separators=(",", ":"))}]}
        except ToolError as e:
            return self._error(str(e))
        except Exception as e:  # noqa: BLE001 - report it helpfully instead of failing the connection
            log.exception("MCP tool %s failed", name)
            return self._error(f"Wick hit an unexpected problem ({clean(type(e).__name__)}: {clean(e, 200)}). "
                               "Nothing more was changed. The log in Wick's Settings has details.")

    @staticmethod
    def _error(text: str) -> dict:
        return {"content": [{"type": "text", "text": text}], "isError": True}

    def _rate_limit(self, name: str) -> None:
        calls, per = RATE_LIMITS[name]
        now = time.monotonic()
        q = self._calls.setdefault(name, deque())
        while q and now - q[0] > per:
            q.popleft()
        if len(q) >= calls:
            wait = int(per - (now - q[0])) + 1
            raise ToolError(f"Too many {name} calls; try again in {wait} seconds.")
        q.append(now)

    def _need_folders(self) -> None:
        if not self.allowed:
            raise ToolError("No folders are allowed yet. The user can choose folders in the extension's settings"
                            + (" (refused: " + "; ".join(f"{p}: {w}" for p, w in self.refused) + ")."
                               if self.refused else "."))

    def t_list_allowed_folders(self, args, **_) -> dict:
        from . import presets

        out: dict[str, Any] = {"allowed_folders": [clean(a) for a in self.allowed],
               "originals": (f"moved to the {BIN_NAME}; undo with undo_run" if self.replaces_originals() else
                             "kept; compressed copies saved next to them"),
               "confirmation": "the user approves every change in a Wick dialog",
               "max_files_per_run": self.max_files, "default_preset": DEFAULT_PRESET,
               "presets": {n: clean(presets.description(n), 200) for n in presets.names()}}
        if self.refused:
            out["refused_folders"] = [{"folder": p, "why": w} for p, w in self.refused]
        if not self.keep_originals and not self.replaces_originals():
            out["note"] = "History is turned off in Wick, so originals are kept (nothing could be undone)."
        return out

    def t_plan(self, args, **_) -> dict:
        from . import presets
        from .formats import human
        from .plan import plan_entry
        from .scanner import scan

        self._need_folders()
        paths = args.get("paths")
        if not isinstance(paths, list) or not 1 <= len(paths) <= 20:
            raise ToolError("paths must be a list of 1 to 20 folders or files.")
        preset = args.get("preset", DEFAULT_PRESET)
        if preset not in presets.names():
            raise ToolError(f"Unknown preset {clean(preset, 60)!r}. Choose one of: {', '.join(presets.names())}.")
        listed = args.get("list_files", 10)
        if not isinstance(listed, int) or isinstance(listed, bool) or not 0 <= listed <= 100:
            raise ToolError("list_files must be a whole number from 0 to 100.")
        roots = [self._check_path(p) for p in paths]
        s = self._settings(preset)
        result = scan(roots, s, picked=False)
        entries = [e for e in result.entries if self._usable(e.path)]
        planned = [(e, plan_entry(e, s)) for e in entries]
        todo = [(e, p) for e, p in planned if p.convert]
        skipped = [p.reason for _, p in planned if not p.convert]
        if len(todo) > self.max_files:
            raise ToolError(f"That's {len(todo)} files; a run can have at most {self.max_files} (the user's setting). "
                            "Plan a smaller folder, or a few subfolders at a time.")
        plan_id = ""
        if todo:
            plan_id = secrets.token_hex(8)
            stamps = {}
            for e, _plan in todo:
                st = e.path.stat()
                stamps[str(e.path)] = (st.st_size, st.st_mtime_ns)
            with self._plans_lock:
                self._expire()
                self._plans[plan_id] = _Plan(plan_id, time.time(), s, result.root, todo, stamps, preset)
        total, est = sum(e.size for e, _ in todo), sum(p.est_size for _, p in todo)
        kinds: dict[str, dict] = {}
        for e, p in todo:
            k = kinds.setdefault(e.kind, {"files": 0, "size": 0, "estimated_after": 0})
            k["files"] += 1
            k["size"] += e.size
            k["estimated_after"] += p.est_size
        reasons: dict[str, int] = {}
        for r in skipped:
            reasons[clean(r, 80)] = reasons.get(clean(r, 80), 0) + 1
        out = {"plan_id": plan_id or None, "preset": preset, "files_to_compress": len(todo),
               "size": human(total), "estimated_after": human(est), "estimated_saving": human(total - est),
               "by_kind": {k: {"files": v["files"], "size": human(v["size"]),
                               "estimated_after": human(v["estimated_after"])} for k, v in kinds.items()},
               "originals": (f"will be moved to the {BIN_NAME} (undo with undo_run)" if self.replaces_originals()
                             else "will be kept; compressed copies are saved next to them"),
               "lossy": sorted({e.kind for e, p in todo if not p.lossless}),
               "files_left_alone": len(skipped),
               "why_left_alone": dict(sorted(reasons.items(), key=lambda kv: -kv[1])[:5])}
        if plan_id:
            out["expires_in_minutes"] = PLAN_LIFETIME // 60
            out["next"] = ("If the user wants this, call compress_planned_files with this plan_id; the user then "
                           "confirms it in Wick.")
        if listed:
            biggest = sorted(todo, key=lambda ep: -ep[0].size)[:listed]
            out["largest_files"] = [{"path": clean(e.path), "size": human(e.size),
                                     "estimated_after": human(p.est_size), "how": clean(p.reason, 80)}
                                    for e, p in biggest]
        if result.skipped_program_dirs:
            out["program_folders_skipped"] = len(result.skipped_program_dirs)
        if result.cloud_only_files:
            out["cloud_only_files_skipped"] = result.cloud_only_files
        return out

    def _expire(self) -> None:
        """(Called with _plans_lock held.)"""
        now = time.time()
        for pid in [k for k, p in self._plans.items() if now - p.created > PLAN_LIFETIME]:
            del self._plans[pid]
        while len(self._plans) >= 10:                       # (keep only the latest few)
            del self._plans[min(self._plans, key=lambda k: self._plans[k].created)]

    def t_compress(self, args, request_id=None, progress_token=None, **_) -> dict:
        from .engine import DONE, FAILED, JobEvents, run_job
        from .formats import human

        plan_id = args.get("plan_id")
        if not isinstance(plan_id, str):
            raise ToolError("plan_id must be the string returned by plan_compression.")
        if not self._job_lock.acquire(blocking=False):
            raise ToolError("Wick is already working on a run for you; wait for it to finish.")
        try:
            with self._plans_lock:
                self._expire()
                plan = self._plans.pop(plan_id, None)       # single use, whatever happens next
            if plan is None:
                raise ToolError("No such plan (plans expire after 30 minutes and can be used once). Call "
                                "plan_compression again.")
            todo, changed = [], 0
            for e, p in plan.items:
                try:
                    st = e.path.stat()
                    same = plan.stamps.get(str(e.path)) == (st.st_size, st.st_mtime_ns)
                except OSError:
                    same = False
                if same and self._usable(e.path):
                    todo.append((e, p))
                else:
                    changed += 1
            if not todo:
                raise ToolError("Every file in the plan has changed or gone since it was made. Nothing was changed.")
            total, est = sum(e.size for e, _ in todo), sum(p.est_size for _, p in todo)
            folders = sorted({clean(e.root or e.path.parent, 120) for e, _ in todo})
            text = (f"An AI assistant, through Wick's agent connection, wants to compress {len(todo)} "
                    f"files ({human(total)}, about {human(est)} afterwards) with the \"{plan.preset}\" preset in:\n\n"
                    + "\n".join(folders[:6]) + (f"\n…and {len(folders) - 6} more" if len(folders) > 6 else "")
                    + "\n\n" + (f"Originals will be moved to the {BIN_NAME}, and this can be undone from History."
                                if plan.settings.output_mode == "replace" else
                                "Originals will be kept; compressed copies are saved next to them.")
                    + "\n\nAllow this?")
            if not self.confirm(text, request_id):
                return {"done": False, "result": "The user didn't approve this, so nothing was changed. Don't try "
                                                 "again unless they ask."}
            cancel = self._cancels.get(request_id) or threading.Event()
            server = self

            class Events(JobEvents):
                def __init__(self):
                    self.n = 0
                    self.lock = threading.Lock()

                def file_finished(self, r):
                    with self.lock:
                        self.n += 1
                        server._progress(progress_token, self.n, len(todo), f"{self.n} of {len(todo)} files")

            results, run_id = run_job(todo, plan.settings, plan.root, Events(), cancel, tool="AI assistant (MCP)")
            done = [r for r in results if r.status == DONE]
            failed = [r for r in results if r.status == FAILED]
            out = {"done": True, "compressed": len(done), "failed": len(failed),
                   "left_alone": len(results) - len(done) - len(failed),
                   "saved": human(sum(r.saved for r in results)), "run_id": run_id}
            if changed:
                out["skipped_changed_since_planning"] = changed
            if cancel.is_set():
                out["cancelled"] = True
            if failed:
                out["failures"] = [{"path": clean(r.entry.path), "why": clean(r.message, 160)} for r in failed[:5]]
            if run_id:
                out["undo"] = ("undo_run with this run_id restores the originals (while they're still in the "
                               f"{BIN_NAME})" if plan.settings.output_mode == "replace" else
                               "originals were kept; undo_run removes the compressed copies")
            return out
        finally:
            self._job_lock.release()

    def t_history(self, args, **_) -> dict:
        from . import journal
        from .formats import human

        limit = args.get("limit", 5)
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50:
            raise ToolError("limit must be a whole number from 1 to 50.")
        runs = [r for r in journal.list_runs() if self._usable(Path(r.root))][:limit]
        return {"runs": [{"run_id": r.run_id, "date": time.strftime("%Y-%m-%d %H:%M", time.localtime(r.started)),
                          "folder": clean(r.root), "files": r.active_files, "saved": human(r.saved_bytes),
                          "by_assistant": r.settings.get("tool") == "AI assistant (MCP)",
                          "undone_files": sum(f.undone for f in r.files)} for r in runs]}

    def t_undo(self, args, request_id=None, progress_token=None, **_) -> dict:
        from . import journal
        from .engine import undo_run
        from .formats import human

        run_id = args.get("run_id")
        if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,80}", run_id):
            raise ToolError("run_id must be a run_id from list_history.")
        run = journal.get_run(run_id)
        if run is None:
            raise ToolError("No such run. list_history shows the runs that can be undone.")
        active = [f for f in run.files if not f.undone]
        if not active:
            raise ToolError("That run has already been undone.")
        if not all(self._usable(Path(f.original)) and self._usable(Path(f.output)) for f in active):
            raise ToolError("That run includes files outside the folders Wick may use for you; the user can "
                            "undo it in Wick's History.")
        edited = []
        for f in active:                  # (undo removes the compressed file: never one that was changed since)
            try:
                if Path(f.output).stat().st_size != f.output_size:
                    edited.append(f)
            except OSError:
                pass
        if edited:
            raise ToolError(f"{len(edited)} compressed file(s) in that run were changed after compressing (e.g. "
                            f"{clean(edited[0].output, 160)}), and undoing would remove those changes. The user can "
                            "undo it in Wick's History, file by file.")
        if not self._job_lock.acquire(blocking=False):
            raise ToolError("Wick is already working on a run for you; wait for it to finish.")
        try:
            text = (f"An AI assistant, through Wick's agent connection, wants to undo a compression run from "
                    f"{time.strftime('%Y-%m-%d %H:%M', time.localtime(run.started))} in:\n\n{clean(run.root, 120)}\n\n"
                    f"{len(active)} originals ({human(sum(f.original_size for f in active))}) will be put back and "
                    "their compressed versions removed.\n\nAllow this?")
            if not self.confirm(text, request_id):
                return {"done": False, "result": "The user didn't approve this, so nothing was changed."}
            n = [0]

            def report(rec, ok, msg):
                n[0] += 1
                self._progress(progress_token, n[0], len(active), f"{n[0]} of {len(active)} files")

            ok, failed = undo_run(run_id, report)
            return {"done": True, "restored": ok, "failed": failed}
        finally:
            self._job_lock.release()

    # ---------------------------------------------------------------- the person's approval
    def confirm(self, text: str, request_id=None) -> bool:
        """Ask the person, outside the agent's reach. True only for an explicit yes."""
        if self._confirm is not None:
            return bool(self._confirm(text))
        if os.name == "nt":
            return _windows_confirm(text, self._cancels.get(request_id))
        if self.client_caps.get("elicitation") is not None:
            return self._elicit(text)
        raise ToolError("Wick can't ask the user for approval here (this client doesn't support "
                        "confirmation requests), so it won't change any files.")

    def _elicit(self, text: str) -> bool:
        response = self.request("elicitation/create", {
            "message": text,
            "requestedSchema": {"type": "object", "required": ["approve"], "properties": {
                "approve": {"type": "boolean", "title": "Allow this", "description": "Tick to let Wick go ahead.",
                            "default": False}}}}, timeout=CONFIRM_TIMEOUT)
        result = (response or {}).get("result") or {}
        return result.get("action") == "accept" and (result.get("content") or {}).get("approve") is True

    # ---------------------------------------------------------------- JSON-RPC
    def send(self, message: dict) -> None:
        data = (json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._write_lock:
            self._out.write(data)
            self._out.flush()

    def request(self, method: str, params: dict, timeout: float):
        with self._write_lock:
            self._next_id += 1
            rid = f"wick-{self._next_id}"
        slot = [threading.Event(), None]
        self._pending[rid] = slot
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        slot[0].wait(timeout)
        self._pending.pop(rid, None)
        return slot[1]

    def _progress(self, token, done: int, total: int, message: str) -> None:
        if token is not None:
            self.send({"jsonrpc": "2.0", "method": "notifications/progress",
                       "params": {"progressToken": token, "progress": done, "total": total, "message": message}})

    def handle(self, msg) -> None:
        """One incoming message (requests that take long run on their own thread)."""
        if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0":
            self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32600, "message": "Invalid request"}})
            return
        if "method" not in msg:                             # a response to one of our requests
            slot = self._pending.get(msg.get("id"))
            if slot is not None:
                slot[1] = msg
                slot[0].set()
            return
        method, rid, params = msg["method"], msg.get("id"), msg.get("params") or {}
        if rid is None:                                     # notifications
            if method == "notifications/cancelled":
                ev = self._cancels.get(params.get("requestId"))
                if ev is not None:
                    ev.set()
            return
        try:
            if method == "initialize":
                asked = params.get("protocolVersion")
                self.client_caps = params.get("capabilities") or {}
                self.reply(rid, {"protocolVersion": asked if asked in PROTOCOL_VERSIONS else PROTOCOL_VERSIONS[0],
                                 "capabilities": {"tools": {"listChanged": False}},
                                 "serverInfo": {"name": "wick", "title": APP_NAME, "version": __version__},
                                 "instructions": (
                                     "Wick shrinks photos, videos, music and documents on this computer. Plan first "
                                     "(plan_compression, which changes nothing), tell the user what it found, and only "
                                     "compress or undo when they ask; they confirm every change in Wick.")})
            elif method == "ping":
                self.reply(rid, {})
            elif method == "tools/list":
                self.reply(rid, {"tools": self.tools()})
            elif method == "tools/call":
                name = params.get("name")
                if name not in {t["name"] for t in self.tools()}:
                    self.send({"jsonrpc": "2.0", "id": rid,
                               "error": {"code": -32602, "message": f"Unknown tool: {clean(name, 80)}"}})
                    return
                self._cancels[rid] = threading.Event()
                token = (params.get("_meta") or {}).get("progressToken")

                def run():
                    try:
                        self.reply(rid, self.call_tool(name, params.get("arguments") or {}, rid, token))
                    finally:
                        self._cancels.pop(rid, None)

                t = threading.Thread(target=run, daemon=True, name=f"mcp-{name}")
                self._threads = [x for x in self._threads if x.is_alive()] + [t]
                t.start()
            else:
                self.send({"jsonrpc": "2.0", "id": rid,
                           "error": {"code": -32601, "message": f"Method not found: {clean(method, 80)}"}})
        except Exception as e:  # noqa: BLE001
            self.send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32603, "message": clean(e, 200)}})

    def reply(self, rid, result: dict) -> None:
        self.send({"jsonrpc": "2.0", "id": rid, "result": result})

    def serve(self, stream) -> None:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except ValueError:
                self.send({"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
                continue
            for m in (msg if isinstance(msg, list) else [msg]):
                self.handle(m)
        # The client closed the connection: stop work in progress (a run stops after the files it's on), and send
        # the replies of calls that finish before leaving.
        for ev in list(self._cancels.values()):
            ev.set()
        for t in list(self._threads):
            t.join(timeout=120)


def _windows_confirm(text: str, cancel: threading.Event | None) -> bool:
    """Wick's own dialog: OK / Cancel, Cancel focused, always on top. Closed (as Cancel) after 10 minutes
    or when the request is cancelled. The agent can't answer it."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32  # type: ignore[attr-defined]
    user32.MessageBoxW.argtypes = (wintypes.HWND, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.UINT)
    user32.FindWindowW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR)
    user32.FindWindowW.restype = wintypes.HWND
    user32.PostMessageW.argtypes = (wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
    title = f"{APP_NAME}: allow this? ({secrets.token_hex(3)})"
    done = threading.Event()

    def watchdog():
        deadline = time.monotonic() + CONFIRM_TIMEOUT
        while not done.wait(0.5):
            if time.monotonic() > deadline or (cancel is not None and cancel.is_set()):
                hwnd = user32.FindWindowW(None, title)
                if hwnd:
                    user32.PostMessageW(hwnd, 0x0010, 0, 0)      # WM_CLOSE: the same as Cancel
                return

    threading.Thread(target=watchdog, daemon=True).start()
    MB_OKCANCEL, MB_ICONWARNING, MB_DEFBUTTON2 = 0x1, 0x30, 0x100
    MB_SETFOREGROUND, MB_TOPMOST = 0x10000, 0x40000
    try:
        answer = user32.MessageBoxW(None, text + "\n\n(OK to go ahead, Cancel to leave everything as it is.)", title,
                                    MB_OKCANCEL | MB_ICONWARNING | MB_DEFBUTTON2 | MB_SETFOREGROUND | MB_TOPMOST)
    finally:
        done.set()
    return answer == 1                                       # IDOK


def _std_streams():
    """Binary stdin/stdout. The windowed app has no Python std streams, but Windows still hands it the pipes the
    client created: open those directly."""
    if sys.stdin is not None and sys.stdout is not None:
        return sys.stdin.buffer, sys.stdout.buffer
    import ctypes
    import msvcrt  # type: ignore[import-not-found]

    k = ctypes.windll.kernel32  # type: ignore[attr-defined]
    k.GetStdHandle.restype = ctypes.c_void_p
    fin = msvcrt.open_osfhandle(k.GetStdHandle(-10), os.O_RDONLY | getattr(os, "O_BINARY", 0))
    fout = msvcrt.open_osfhandle(k.GetStdHandle(-11), os.O_WRONLY | getattr(os, "O_BINARY", 0))
    return os.fdopen(fin, "rb"), os.fdopen(fout, "wb")


def parse_args(argv: list[str]) -> tuple[list[str], bool, int]:
    """``[--keep-originals[=true|false]] [--max-files=N] FOLDER...`` (the MCPB host passes booleans as text)."""
    folders, keep, max_files = [], False, DEFAULT_MAX_FILES
    it = iter(argv)
    for a in it:
        key, _, value = a.partition("=")
        if key == "--keep-originals":
            if not _ and value == "":
                nxt = next(it, "true")
                if nxt.lower() in ("true", "false", "1", "0"):
                    value = nxt
                else:
                    folders.append(nxt)
                    value = "true"
            keep = value.lower() not in ("false", "0", "")
        elif key == "--max-files":
            try:
                max_files = int(value if _ else next(it, ""))
            except ValueError:
                pass
        elif a == "--":
            continue
        else:
            folders.append(a)
    return folders, keep, max_files


def main(argv: list[str]) -> int:
    from .log import setup as setup_log

    stdin, stdout = _std_streams()
    sys.stdout = sys.stderr if sys.stderr is not None else open(os.devnull, "w")  # noqa: SIM115 - protocol only
    setup_log()
    folders, keep, max_files = parse_args(argv)
    server = Server(folders, keep_originals=keep, max_files=max_files, out=stdout)
    server.serve(stdin)
    return 0


__all__ = ["Server", "main", "parse_args", "clean", "forbidden_roots", "PROTOCOL_VERSIONS"]
