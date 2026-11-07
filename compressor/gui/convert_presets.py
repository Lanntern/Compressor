"""Convert presets: the Convert page's choices saved under a name (formats and their settings, where results
go, their names, what happens to the originals). Each one is offered in Explorer's "Convert with Compressor"
menu for the kinds of file it converts.

Stored as JSON in the app's settings ("convert/presets"). A PDF password is never part of a preset.
"""

from __future__ import annotations

import dataclasses
import json

from .. import convert

KEY = "convert/presets"
# Kind of file (as in Explorer's menus) -> the option that says what it becomes.
KIND_FIELDS = {"image": "image_target", "video": "video_target", "audio": "audio_target"}


def load(qs) -> dict[str, dict]:
    try:
        data = json.loads(str(qs.value(KEY, "{}", str)) or "{}")
    except ValueError:
        return {}
    return {str(k): v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}


def save(qs, presets: dict[str, dict]) -> None:
    qs.setValue(KEY, json.dumps(presets, ensure_ascii=False, sort_keys=True))


def make(opts: convert.ConvertOptions, output: dict) -> dict:
    o = dataclasses.asdict(opts)
    o.pop("pdf_password", None)
    o["ico_sizes"] = list(opts.ico_sizes)
    return {"opts": o, "output": dict(output)}


def options(preset: dict) -> convert.ConvertOptions:
    fields = {f.name for f in dataclasses.fields(convert.ConvertOptions)}
    o = {k: v for k, v in (preset.get("opts") or {}).items() if k in fields and k != "pdf_password"}
    if "ico_sizes" in o:
        o["ico_sizes"] = tuple(int(x) for x in o["ico_sizes"])
    return convert.ConvertOptions(**o)


def kinds(preset: dict) -> list[str]:
    """The kinds of file (image, video, audio) this preset converts."""
    o = options(preset)
    return [k for k, field in KIND_FIELDS.items() if getattr(o, field) not in ("none", "")]


def describe(preset: dict) -> str:
    o, out = options(preset), preset.get("output") or {}
    parts = []
    for kind, field in KIND_FIELDS.items():
        target = getattr(o, field)
        tables: dict[str, dict] = {"image": convert.IMAGE_TARGETS, "video": convert.VIDEO_TARGETS,
                                   "audio": convert.AUDIO_TARGETS}
        table = tables[kind]
        if target in table:
            parts.append(f"{kind}s to {table[target][0]}")
    where = {"beside": "next to the originals", "suffix": "with a new name", "folder": "in a folder",
             "replace": "replacing the originals"}.get(out.get("output_mode", "beside"), "")
    text = "; ".join(parts) or "nothing converted"
    if where:
        text += f", saved {where}"
    if out.get("originals_to") and out.get("output_mode") != "replace":
        text += ", originals moved to a folder"
    return text[0].upper() + text[1:]


__all__ = ["load", "save", "make", "options", "kinds", "describe", "KIND_FIELDS"]
