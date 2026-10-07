"""Short task handles and Discord button ids — pure helpers, no I/O.

Kept out of main.py so they're unit-testable without Discord/Google/apscheduler.
A short name is the handle shown as `[endcard1]` in every DM and accepted as an
exact task reference. Decided 2026-10-06 (reversing the earlier alias:: rejection):
derived from the Belki id by default so most cards need nothing, with an optional
`short::` override for cards whose id slug is random.
"""

import re

ACTIONS = ("done", "keep", "push")
CUSTOM_ID_RE = re.compile(r"^tether:(?P<action>done|keep|push):(?P<eid>[A-Za-z0-9_-]+)$")


def derive_short(belki_id: str | None, override: str | None) -> str | None:
    if override and override.strip():
        return override.strip().lower()
    if not belki_id:
        return None
    parts = belki_id.split("-")
    if len(parts) >= 3:
        return parts[1].lower()
    # Legacy/odd ids: drop a leading "task-" and use the rest.
    return (parts[1] if len(parts) == 2 and parts[0] == "task" else belki_id).lower()


def normalize_query(q: str) -> str:
    return (q or "").strip().strip("[]").strip().lower()


def find_collisions(pairs: list[tuple[str, str]]) -> dict[str, list[str]]:
    seen: dict[str, list[str]] = {}
    for short, name in pairs:
        if short:
            seen.setdefault(short, []).append(name)
    return {s: names for s, names in seen.items() if len(names) > 1}


def button_custom_id(action: str, event_id: str) -> str:
    return f"tether:{action}:{event_id}"


def parse_custom_id(cid: str) -> tuple[str, str] | None:
    m = CUSTOM_ID_RE.match(cid or "")
    return (m.group("action"), m.group("eid")) if m else None
