"""Developer settings: hidden unless switched on, either by starting with COMPRESSOR_DEV=1 or by clicking the
version line in About seven times (click it seven more times to hide them again)."""

from __future__ import annotations

import os

KEY = "dev/enabled"
CLICKS = 7


def enabled(qs) -> bool:
    return os.environ.get("COMPRESSOR_DEV", "") not in ("", "0") or bool(qs.value(KEY, False, bool))


def toggle(qs) -> bool:
    """Flip the remembered switch; returns whether developer settings are now on."""
    qs.setValue(KEY, not qs.value(KEY, False, bool))
    return enabled(qs)


def apply(qs) -> None:
    """Apply the developer settings at start-up (each is off whenever developer settings are hidden)."""
    from . import style

    style.use_fluent_icons(enabled(qs) and bool(qs.value("dev/fluent_icons", False, bool)))


__all__ = ["enabled", "toggle", "apply", "CLICKS"]
