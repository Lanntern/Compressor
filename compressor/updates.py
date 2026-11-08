"""Is a newer Compressor available? (Its bundled decoders get security fixes; a new build carries them.)

One anonymous HTTPS request to GitHub's list of releases: no account, no identifier, nothing about the
user or their files. GitHub sees the IP address, like any website. Nothing is downloaded or installed:
the app only says a newer version exists and links to it.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass

from . import __version__

REPO = "Lanntern/Compressor"
API = f"https://api.github.com/repos/{REPO}/releases/latest"
PAGE = f"https://github.com/{REPO}/releases/latest"
EVERY_DAYS = 7      # (the default: weekly)

# How often to check: the choice in Settings and in the installer ("updates/frequency"). Days between checks;
# 0 = every time the app opens, None = never.
FREQUENCIES: dict[str, tuple[str, int | None]] = {
    "launch": ("Whenever I open Compressor", 0),
    "weekly": ("Weekly (recommended)", 7),
    "monthly": ("Monthly", 30),
    "never": ("Never", None),
}
DEFAULT_FREQUENCY = "weekly"
HELP = ("Compressor opens your files with bundled decoders (ffmpeg, image and PDF libraries) that regularly get "
        "security fixes. It asks GitHub whether a newer Compressor exists (weekly unless you choose otherwise), so "
        "you hear about them. "
        "Only the version number is fetched: nothing about you or your files is sent (GitHub sees your IP "
        "address, like any website). Nothing is downloaded or installed by itself.")


class CheckFailed(Exception):
    pass


@dataclass
class Release:
    version: str
    url: str
    security: bool        # the release notes mention security fixes
    notes: str


def parse(version: str) -> tuple[int, ...]:
    """"v1.2.10" → (1, 2, 10); anything after the numbers (-beta…) is ignored."""
    m = re.match(r"v?(\d+(?:\.\d+)*)", version.strip())
    return tuple(int(x) for x in m.group(1).split(".")) if m else ()


def newer(candidate: str, current: str = __version__) -> bool:
    a, b = parse(candidate), parse(current)
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) > b + (0,) * (width - len(b))


def latest(timeout: float = 15) -> Release:
    req = urllib.request.Request(API, headers={"Accept": "application/vnd.github+json",
                                               "User-Agent": f"Compressor/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise CheckFailed("No published releases to compare with yet") from e
        if e.code in (403, 429):
            raise CheckFailed("GitHub asked to try again later") from e
        raise CheckFailed(f"GitHub answered {e.code}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise CheckFailed("No connection to GitHub") from e
    except ValueError as e:
        raise CheckFailed("GitHub's answer couldn't be read") from e
    tag = str(data.get("tag_name") or "")
    if not parse(tag):
        raise CheckFailed("The latest release has no version number")
    notes = str(data.get("body") or "")
    security = bool(re.search(r"\bsecurity\b|\bCVE-\d{4}-\d+", notes + " " + str(data.get("name") or ""), re.I))
    return Release(tag.lstrip("v"), str(data.get("html_url") or PAGE), security, notes)


def check(timeout: float = 15) -> Release | None:
    """The newer release, or None when this is the newest. Raises CheckFailed."""
    rel = latest(timeout)
    return rel if newer(rel.version) else None


def frequency(qs) -> str:
    """The chosen check frequency, from QSettings ``qs``. Settings written before the choice existed kept only an
    on/off switch ("updates/auto"): off means never, on means the weekly default."""
    value = str(qs.value("updates/frequency", "") or "")
    if value in FREQUENCIES:
        return value
    if qs.contains("updates/auto"):
        return DEFAULT_FREQUENCY if qs.value("updates/auto", True, bool) else "never"
    return DEFAULT_FREQUENCY


def due(freq: str, last_check: float, now: float) -> bool:
    """Whether an automatic check should run now (``last_check``: unix time of the last successful one, 0 if none)."""
    days = FREQUENCIES.get(freq, FREQUENCIES[DEFAULT_FREQUENCY])[1]
    if days is None:
        return False
    return days == 0 or now - last_check > days * 86400


__all__ = ["check", "latest", "newer", "parse", "Release", "CheckFailed", "HELP", "PAGE", "EVERY_DAYS",
           "FREQUENCIES", "DEFAULT_FREQUENCY", "frequency", "due"]
