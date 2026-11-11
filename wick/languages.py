"""Languages: which ones the user reads and listens to, for keeping only those audio and subtitle tracks.

Video files tag tracks with ISO 639-2 codes ("eng", "swe", and for some languages two spellings: "ger"/"deu").
The user's languages are guessed from the system: display languages (Windows' preferred UI languages,
LANGUAGE/LC_*/LANG on Linux) and installed keyboard layouts (someone with a Swedish keyboard reads
Swedish), until they pick their own.
"""

from __future__ import annotations

import os
import subprocess
import sys

# ISO 639-1 → (ISO 639-2 codes as they appear in files, English name). The first code is the usual tag.
LANGUAGES: dict[str, tuple[tuple[str, ...], str]] = {
    "ar": (("ara",), "Arabic"), "bg": (("bul",), "Bulgarian"), "bn": (("ben",), "Bengali"),
    "ca": (("cat",), "Catalan"), "cs": (("cze", "ces"), "Czech"), "cy": (("wel", "cym"), "Welsh"),
    "da": (("dan",), "Danish"), "de": (("ger", "deu"), "German"), "el": (("gre", "ell"), "Greek"),
    "en": (("eng",), "English"), "es": (("spa",), "Spanish"), "et": (("est",), "Estonian"),
    "eu": (("baq", "eus"), "Basque"), "fa": (("per", "fas"), "Persian"), "fi": (("fin",), "Finnish"),
    "fil": (("fil", "tgl"), "Filipino"), "fr": (("fre", "fra"), "French"), "ga": (("gle",), "Irish"),
    "gl": (("glg",), "Galician"), "he": (("heb",), "Hebrew"), "hi": (("hin",), "Hindi"),
    "hr": (("hrv",), "Croatian"), "hu": (("hun",), "Hungarian"), "hy": (("arm", "hye"), "Armenian"),
    "id": (("ind",), "Indonesian"), "is": (("ice", "isl"), "Icelandic"), "it": (("ita",), "Italian"),
    "ja": (("jpn",), "Japanese"), "ka": (("geo", "kat"), "Georgian"), "kk": (("kaz",), "Kazakh"),
    "ko": (("kor",), "Korean"), "lt": (("lit",), "Lithuanian"), "lv": (("lav",), "Latvian"),
    "mk": (("mac", "mkd"), "Macedonian"), "ms": (("may", "msa"), "Malay"), "mt": (("mlt",), "Maltese"),
    "nb": (("nob", "nor"), "Norwegian"), "nl": (("dut", "nld"), "Dutch"), "pl": (("pol",), "Polish"),
    "pt": (("por",), "Portuguese"), "ro": (("rum", "ron"), "Romanian"), "ru": (("rus",), "Russian"),
    "sk": (("slo", "slk"), "Slovak"), "sl": (("slv",), "Slovenian"), "sq": (("alb", "sqi"), "Albanian"),
    "sr": (("srp",), "Serbian"), "sv": (("swe",), "Swedish"), "sw": (("swa",), "Swahili"),
    "ta": (("tam",), "Tamil"), "te": (("tel",), "Telugu"), "th": (("tha",), "Thai"),
    "tr": (("tur",), "Turkish"), "uk": (("ukr",), "Ukrainian"), "ur": (("urd",), "Urdu"),
    "vi": (("vie",), "Vietnamese"), "zh": (("chi", "zho", "cmn", "yue"), "Chinese"),
}
_ALIASES = {"no": "nb", "nn": "nb", "iw": "he", "in": "id", "tl": "fil", "zh-hant": "zh", "zh-hans": "zh"}
# Keyboard layouts (XKB names on Linux) that say which language someone types.
_XKB = {"us": "en", "gb": "en", "ie": "en", "au": "en", "ca": "fr", "de": "de", "at": "de", "ch": "de",
        "fr": "fr", "be": "fr", "es": "es", "latam": "es", "it": "it", "pt": "pt", "br": "pt", "nl": "nl",
        "se": "sv", "no": "nb", "dk": "da", "fi": "fi", "is": "is", "pl": "pl", "cz": "cs", "sk": "sk",
        "hu": "hu", "ro": "ro", "bg": "bg", "gr": "el", "ru": "ru", "ua": "uk", "by": "ru", "tr": "tr",
        "il": "he", "ara": "ar", "ir": "fa", "in": "hi", "jp": "ja", "kr": "ko", "cn": "zh", "tw": "zh",
        "th": "th", "vn": "vi", "lt": "lt", "lv": "lv", "ee": "et", "si": "sl", "hr": "hr", "rs": "sr",
        "mk": "mk", "al": "sq", "ge": "ka", "am": "hy", "kz": "kk", "pk": "ur", "id": "id", "my": "ms"}


def normalize(code: str) -> str:
    """Any spelling (en, en-GB, en_US.UTF-8, eng, ger, deu) → the ISO 639-1 key used here, or ""."""
    c = code.strip().lower().replace("_", "-").split(".")[0].split("@")[0]
    if not c or c in ("c", "posix"):
        return ""
    if c in _ALIASES:
        return _ALIASES[c]
    base = c.split("-")[0]
    base = _ALIASES.get(base, base)
    if base in LANGUAGES:
        return base
    for key, (codes, _name) in LANGUAGES.items():
        if base in codes:
            return key
    return ""


def name(key: str) -> str:
    return LANGUAGES.get(key, ((), key))[1]


def track_codes(keys: list[str]) -> set[str]:
    """Every tag spelling for these languages (what to compare track tags with)."""
    out: set[str] = set()
    for k in keys:
        if k in LANGUAGES:
            out |= set(LANGUAGES[k][0]) | {k}
    return out


def _windows() -> list[str]:
    import ctypes
    from ctypes import wintypes

    found: list[str] = []
    k32, user32 = ctypes.windll.kernel32, ctypes.windll.user32  # type: ignore[attr-defined]
    # Display languages, in the user's order (Settings → Time & language → Language).
    num, size = wintypes.ULONG(), wintypes.ULONG(0)
    if k32.GetUserPreferredUILanguages(0x8, ctypes.byref(num), None, ctypes.byref(size)) and size.value:
        buf = ctypes.create_unicode_buffer(size.value)
        if k32.GetUserPreferredUILanguages(0x8, ctypes.byref(num), buf, ctypes.byref(size)):
            found += [x for x in buf[:size.value].split("\0") if x]     # MUI_LANGUAGE_NAME: "sv-SE"
    # Installed keyboard layouts: their low word is the input language.
    n = user32.GetKeyboardLayoutList(0, None)
    if n > 0:
        layouts = (ctypes.c_void_p * n)()
        user32.GetKeyboardLayoutList(n, layouts)
        for hkl in layouts:
            lcid = (hkl or 0) & 0xFFFF
            buf = ctypes.create_unicode_buffer(85)
            if lcid and k32.LCIDToLocaleName(lcid, buf, 85, 0):
                found.append(buf.value)
    return found


def _linux() -> list[str]:
    found: list[str] = []
    for var in ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG"):
        found += [x for x in os.environ.get(var, "").split(":") if x]
    layouts = ""
    try:
        with open("/etc/default/keyboard", encoding="utf-8") as f:
            for line in f:
                if line.startswith("XKBLAYOUT="):
                    layouts = line.split("=", 1)[1].strip().strip('"')
    except OSError:
        pass
    if not layouts:
        try:
            out = subprocess.run(["setxkbmap", "-query"], capture_output=True, text=True, timeout=5).stdout
            layouts = next((ln.split(":", 1)[1].strip() for ln in out.splitlines() if ln.startswith("layout")), "")
        except (OSError, subprocess.SubprocessError):
            pass
    found += [_XKB.get(x.strip().lower(), "") for x in layouts.split(",")]
    return found


def detect() -> list[str]:
    """The user's languages from this computer, most important first (English if nothing is found)."""
    try:
        raw = _windows() if sys.platform == "win32" else _linux()
    except Exception:  # noqa: BLE001 - detection is a convenience
        raw = []
    out: list[str] = []
    for code in raw:
        k = normalize(code)
        if k and k not in out:
            out.append(k)
    return out or ["en"]


def parse(text: str) -> list[str]:
    """A saved list ("sv,en") back to keys; unknown entries are dropped."""
    out: list[str] = []
    for part in (text or "").split(","):
        k = normalize(part)
        if k and k not in out:
            out.append(k)
    return out


def describe(keys: list[str]) -> str:
    return ", ".join(name(k) for k in keys) or "none"


__all__ = ["LANGUAGES", "detect", "parse", "describe", "normalize", "name", "track_codes"]
