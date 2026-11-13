"""Developer settings: hidden unless switched on, either by starting with WICK_DEV=1 or by clicking the
version line in About seven times (click it seven more times to hide them again)."""

from __future__ import annotations

import os

KEY = "dev/enabled"
CLICKS = 7


def enabled(qs) -> bool:
    return os.environ.get("WICK_DEV", "") not in ("", "0") or bool(qs.value(KEY, False, bool))


def toggle(qs) -> bool:
    """Flip the remembered switch; returns whether developer settings are now on."""
    qs.setValue(KEY, not qs.value(KEY, False, bool))
    return enabled(qs)


def apply(qs) -> None:
    """Apply the developer settings at start-up (each is off whenever developer settings are hidden)."""
    from . import style

    # Windows' own icons (Segoe Fluent / MDL2) by default on Windows; the app's drawn icons elsewhere, or when
    # turned off in the developer settings.
    style.use_fluent_icons(bool(qs.value("dev/fluent_icons", fluent_default(), bool)))


def fluent_default() -> bool:
    return os.name == "nt"


__all__ = ["enabled", "toggle", "apply", "CLICKS"]
