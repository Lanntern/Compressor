"""Small, UI-independent model for simultaneous work. Unknown progress stays unknown."""

from dataclasses import dataclass


@dataclass
class Activity:
    key: str
    name: str
    stage: str
    fraction: float | None = None
    paused: bool = False
    elapsed: float = 0
    page: str = "home"
    section: int | None = None


def overall(tasks: list[Activity]) -> tuple[float | None, bool]:
    if not tasks:
        return None, False
    known = [max(0.0, min(1.0, t.fraction)) for t in tasks if t.fraction is not None]
    return (sum(known) / len(tasks) if len(known) == len(tasks) else None, all(t.paused for t in tasks))
