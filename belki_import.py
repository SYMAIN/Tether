"""
belki_import.py — imports Belki tasks into Tether's Sunday queue.

Belki (Claude Code sessions writing into the Obsidian vault) owns task
decomposition; Tether owns enforcement. Belki stores tasks in monthly data
files at BELKI_PATH/Data/YYYY-MM.md:

    - [ ] Task title
      id:: task-xxxxxxxx-xxxxxx
      created:: 2026-07-06
      priority:: P2
      project:: Tether
      labels:: bug, infra
      estimate:: 2
      due:: 2026-07-12
      description:: one line

Only tasks carrying a project:: are importable — unassigned tasks are
invisible to Tether. estimate:: is integer evenings and drives capacity
packing (EVENINGS_PER_WEEK per week); a task without an estimate
conservatively fills its whole week. A task with a future due:: keeps that
exact date instead of being packed; a past due:: is repacked.

Exactly one project is active at a time (state key 'active_project' in the
ledger DB). It is chosen explicitly — `@Tether sync belki <name>` — and
stays active while it has open tasks. Sync never switches projects on its
own; with no active project it lists what's available instead of guessing.

Parsing is lenient: unparseable lines are reported in the sync reply, never
fatal. main.py injects its calendar functions into sync() — this module
never talks to Google directly.
"""

import datetime
import os
import re

import ledger
from ledger import clean_name

BELKI_PATH = os.environ.get("BELKI_PATH", "/app/belki")
EVENINGS_PER_WEEK = int(os.environ.get("EVENINGS_PER_WEEK", "4"))

_CHECKBOX_RE = re.compile(r"^- \[( |x|X)\] (.+)$")
_FIELD_RE = re.compile(r"^\s+([A-Za-z_]+)::\s*(.*)$")
_META_EST_RE = re.compile(r"estimate=(\d+)")


def parse_data_file(path: str) -> tuple[list[dict], list[str]]:
    """Parses one monthly Belki data file.

    Returns (tasks, skipped). Each task: {name, id, project, estimate, due,
    description, done, priority}.
    """
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    tasks: list[dict] = []
    skipped: list[str] = []
    current: dict | None = None

    for line in lines:
        match = _CHECKBOX_RE.match(line)
        if match:
            name = match.group(2).strip()
            if not name:
                skipped.append(line.strip())
                current = None
                continue
            current = {
                "name": name,
                "id": None,
                "project": None,
                "estimate": None,
                "due": None,
                "description": "",
                "done": match.group(1).lower() == "x",
                "priority": None,
            }
            tasks.append(current)
            continue
        field = _FIELD_RE.match(line)
        if field and current:
            key, val = field.group(1).lower(), field.group(2).strip()
            if key in ("estimate", "est"):
                try:
                    current["estimate"] = int(val)
                except ValueError:
                    skipped.append(line.strip())
            elif key == "due":
                try:
                    datetime.date.fromisoformat(val)
                    current["due"] = val
                except ValueError:
                    skipped.append(line.strip())
            elif key == "project":
                current["project"] = val
            elif key == "description":
                current["description"] = val
            elif key == "id":
                current["id"] = val
            elif key == "priority":
                current["priority"] = val
            # unknown keys (created::, labels::, completed::) are fine — ignore
            continue
        if line.startswith("- ") and line.strip():
            # top-level list line that isn't a checkbox
            skipped.append(line.strip())
            current = None
            continue
        if line.strip() and line[:1] not in (" ", "\t", "#"):
            current = None

    return tasks, skipped


def load_tasks(belki_path: str = None) -> tuple[list[dict], list[str]]:
    """All tasks from BELKI_PATH/Data/*.md in file order. Duplicate ids keep
    the first occurrence."""
    data_dir = os.path.join(belki_path or BELKI_PATH, "Data")
    if not os.path.isdir(data_dir):
        return [], [f"(no Data folder at {data_dir})"]
    tasks: list[dict] = []
    skipped: list[str] = []
    seen_ids: set[str] = set()
    for fname in sorted(os.listdir(data_dir)):
        if not fname.lower().endswith(".md"):
            continue
        try:
            file_tasks, file_skipped = parse_data_file(os.path.join(data_dir, fname))
        except Exception as e:
            skipped.append(f"({fname} unreadable: {e})")
            continue
        skipped.extend(file_skipped)
        for t in file_tasks:
            if t["id"] and t["id"] in seen_ids:
                continue
            if t["id"]:
                seen_ids.add(t["id"])
            tasks.append(t)
    return tasks, skipped


def open_project_counts(tasks: list[dict]) -> dict:
    """Open-task counts per project name, in first-seen order."""
    counts: dict[str, int] = {}
    for t in tasks:
        if t["project"] and not t["done"]:
            counts[t["project"]] = counts.get(t["project"], 0) + 1
    return counts


def _next_sunday_on_or_after(d: datetime.date) -> datetime.date:
    return d + datetime.timedelta(days=(6 - d.weekday()) % 7)


def week_usage(deadlines: list) -> dict:
    """Evenings already claimed per week, keyed by week-ending Sunday (ISO date).

    A week is a capacity bucket of EVENINGS_PER_WEEK, not a single slot.
    Events without an estimate= in their meta are legacy project-sized tasks
    and conservatively fill their whole week.
    """
    usage: dict[str, int] = {}
    for e in deadlines:
        due = (e.get("start", {}) or {}).get("dateTime", "")[:10]
        if not due:
            continue
        try:
            week = _next_sunday_on_or_after(
                datetime.date.fromisoformat(due)
            ).isoformat()
        except ValueError:
            continue
        m = _META_EST_RE.search(e.get("description", "") or "")
        need = int(m.group(1)) if m else EVENINGS_PER_WEEK
        usage[week] = usage.get(week, 0) + need
    return usage


def _fits(used: int, need: int) -> bool:
    # An empty week always accepts a task, even an oversized one (it then
    # owns the week); a non-empty week only accepts what fits in the bucket.
    return used == 0 or used + need <= EVENINGS_PER_WEEK


def _need(task: dict) -> int:
    return task["estimate"] if task["estimate"] is not None else EVENINGS_PER_WEEK


def sync(
    deadlines: list, insert_deadline, project_override: str = None, dry_run: bool = False
) -> tuple[str, int]:
    """Imports the active project's new tasks, packing weeks by estimate.

    deadlines: current ⏰ queue events (main.get_tether_deadlines()).
    insert_deadline: main._insert_deadline.
    dry_run: don't persist the active-project switch (caller passes a
    non-inserting insert_deadline too).
    Returns (reply text, number imported).
    """
    if not os.path.isdir(BELKI_PATH):
        return (f"⚠️ Belki folder not found at `{BELKI_PATH}`.", 0)

    tasks, skipped = load_tasks()
    counts = open_project_counts(tasks)
    if not counts:
        return ("⚠️ No open Belki tasks with a `project::` found in `Data/*.md`.", 0)

    queue_names = {clean_name(e.get("summary", "")).lower() for e in deadlines}
    done_names = ledger.completed_names()

    def importable(pname: str) -> list[dict]:
        return [
            t
            for t in tasks
            if t["project"]
            and t["project"].lower() == pname.lower()
            and not t["done"]
            and t["name"].lower() not in queue_names
            and t["name"].lower() not in done_names
        ]

    def in_queue(pname: str) -> bool:
        return any(
            t["name"].lower() in queue_names
            for t in tasks
            if t["project"] and t["project"].lower() == pname.lower()
        )

    listing = ", ".join(f"**{p}** ({n} open)" for p, n in counts.items())
    stored = ledger.get_state("active_project")
    if project_override:
        needle = project_override.lower()
        matches = [p for p in counts if needle in p.lower()]
        if not matches:
            return (f"No Belki project matching **{project_override}**. Available: {listing}.", 0)
        active = matches[0]
    else:
        active = next(
            (p for p in counts if stored and p.lower() == stored.lower()), None
        )
        if active is None:
            if stored:
                return (
                    f"✅ **{stored}** has no open Belki tasks left. Pick the next project "
                    f"with `@Tether sync belki <name>` — available: {listing}.",
                    0,
                )
            return (
                f"No active Belki project set. Pick one with `@Tether sync belki <name>` "
                f"— available: {listing}.",
                0,
            )

    new_tasks = importable(active)
    if not new_tasks:
        others = [p for p in counts if p.lower() != active.lower() and importable(p)]
        hint = (
            " Other projects with importable tasks: "
            + ", ".join(f"**{p}**" for p in others)
            + ". Switch with `@Tether sync belki <name>`."
            if others
            else ""
        )
        return (f"Nothing to import — **{active}** has no new tasks.{hint}", 0)

    switched = stored is not None and stored.lower() != active.lower()

    today = datetime.datetime.now(ledger.TORONTO_TZ).date()
    usage = week_usage(deadlines)
    slot = _next_sunday_on_or_after(today + datetime.timedelta(days=1))

    # Fixed-date tasks claim their week's capacity up front so packed tasks
    # flow around them regardless of file order.
    def is_fixed(t: dict) -> bool:
        return bool(t["due"]) and datetime.date.fromisoformat(t["due"]) > today

    for t in new_tasks:
        if is_fixed(t):
            week = _next_sunday_on_or_after(
                datetime.date.fromisoformat(t["due"])
            ).isoformat()
            usage[week] = usage.get(week, 0) + _need(t)

    lines = []
    if switched:
        lines.append(f"🔀 Active project switched to **{active}**.")
    lines.append(f"📥 Imported {len(new_tasks)} task(s) from **{active}**:")

    imported = 0
    for t in new_tasks:
        # Pack by estimated evenings: a week holds EVENINGS_PER_WEEK, not one
        # task. The cursor only moves forward so Belki order (usually a
        # dependency order) is preserved; a task with no estimate fills its
        # whole week.
        need = _need(t)
        note = ""
        if is_fixed(t):
            due = t["due"]
            note = " (fixed due date from Belki)"
        else:
            if t["due"]:
                note = " (listed due date already passed — repacked)"
            while not _fits(usage.get(slot.isoformat(), 0), need):
                slot += datetime.timedelta(days=7)
            due = slot.isoformat()
            usage[due] = usage.get(due, 0) + need
        insert_deadline(
            f"{ledger.DEADLINE_PREFIX} {t['name']} — DUE",
            f"{due}T23:59:00",
            f"{due}T23:59:00",
            origin="belki_import",
            estimate=t["estimate"],
            body_text=t["description"] or None,
            project=active,
        )
        imported += 1
        est_note = (
            f" (est: {t['estimate']} evening{'s' if t['estimate'] != 1 else ''})"
            if t["estimate"] is not None
            else " (no estimate — fills its week)"
        )
        lines.append(f"• {t['name']} — due {due}{est_note}{note}")
        if t["estimate"] is not None and t["estimate"] > EVENINGS_PER_WEEK:
            lines.append(
                f"  ⚠️ estimated {t['estimate']} evenings exceeds your weekly capacity "
                f"of {EVENINGS_PER_WEEK} — consider splitting it in Belki."
            )

    if skipped:
        lines.append("Skipped lines I couldn't parse:")
        lines.extend(f"  ✗ {s}" for s in skipped[:5])

    if not dry_run:
        ledger.set_state("active_project", active)
    return ("\n".join(lines), imported)
