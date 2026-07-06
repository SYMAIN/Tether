"""
belki_import.py — imports Belki subtasks into Tether's Sunday queue.

Belki (Claude Code sessions writing into the Obsidian vault) owns task
decomposition; Tether owns enforcement. One markdown file per project in
BELKI_PATH:

    ---
    deadline: 2026-08-15
    ---
    # Project Name
    - [ ] Subtask name (est: 2)
          optional description line(s), indented
    - [x] Already-done subtask (est: 1)

Estimates are integer evenings. Exactly one project is active at a time
(state key 'active_project' in the ledger DB): kept while it still has open
subtasks in the queue, otherwise the not-yet-finished project with the
earliest frontmatter deadline takes over. A project without a deadline is
only importable by naming it explicitly.

Parsing is lenient: unparseable task lines are reported in the sync reply,
never fatal. main.py injects its calendar functions into sync() — this
module never talks to Google directly.
"""

import datetime
import os
import re

import ledger
from ledger import clean_name

BELKI_PATH = os.environ.get("BELKI_PATH", "/app/belki")

_CHECKBOX_RE = re.compile(r"^- \[( |x|X)\] (.+)$")
_EST_RE = re.compile(r"est[:=]\s*(\d+)", re.IGNORECASE)
_EST_STRIP_RE = re.compile(r"\(?\s*est[:=]\s*\d+\s*\)?", re.IGNORECASE)


def parse_project_file(path: str) -> dict:
    """Returns {name, deadline, subtasks: [{name, estimate, description, done}], skipped}."""
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    deadline = None
    body_start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                body_start = i + 1
                break
            if lines[i].strip().lower().startswith("deadline:"):
                raw = lines[i].split(":", 1)[1].strip()
                try:
                    datetime.date.fromisoformat(raw)
                    deadline = raw
                except ValueError:
                    pass

    name = os.path.splitext(os.path.basename(path))[0]
    subtasks: list[dict] = []
    skipped: list[str] = []
    current: dict | None = None

    for line in lines[body_start:]:
        if line.startswith("# "):
            name = line[2:].strip() or name
            current = None
            continue
        match = _CHECKBOX_RE.match(line)
        if match:
            done = match.group(1).lower() == "x"
            text = match.group(2).strip()
            est_match = _EST_RE.search(text)
            estimate = int(est_match.group(1)) if est_match else None
            task_name = _EST_STRIP_RE.sub("", text).strip(" -—·").strip()
            if not task_name:
                skipped.append(line.strip())
                current = None
                continue
            current = {
                "name": task_name,
                "estimate": estimate,
                "description": "",
                "done": done,
            }
            subtasks.append(current)
            continue
        if line[:1] in (" ", "\t") and line.strip() and current:
            current["description"] = (
                f"{current['description']}\n{line.strip()}".strip()
            )
            continue
        if line.startswith("- ") and line.strip():
            skipped.append(line.strip())
            current = None

    return {"name": name, "deadline": deadline, "subtasks": subtasks, "skipped": skipped}


def list_projects(belki_path: str = None) -> list[dict]:
    path = belki_path or BELKI_PATH
    projects = []
    for fname in sorted(os.listdir(path)):
        if not fname.lower().endswith(".md"):
            continue
        try:
            projects.append(parse_project_file(os.path.join(path, fname)))
        except Exception as e:
            projects.append(
                {
                    "name": os.path.splitext(fname)[0],
                    "deadline": None,
                    "subtasks": [],
                    "skipped": [f"(file unreadable: {e})"],
                }
            )
    return projects


def _next_sunday_on_or_after(d: datetime.date) -> datetime.date:
    return d + datetime.timedelta(days=(6 - d.weekday()) % 7)


def sync(
    deadlines: list, insert_deadline, project_override: str = None, dry_run: bool = False
) -> tuple[str, int]:
    """Imports the active project's new subtasks onto consecutive open Sundays.

    deadlines: current ⏰ queue events (main.get_tether_deadlines()).
    insert_deadline: main._insert_deadline.
    dry_run: don't persist the active-project switch (caller passes a
    non-inserting insert_deadline too).
    Returns (reply text, number imported).
    """
    if not os.path.isdir(BELKI_PATH):
        return (f"⚠️ Belki folder not found at `{BELKI_PATH}`.", 0)

    projects = list_projects()
    if not projects:
        return ("⚠️ No project files in the Belki folder.", 0)

    queue_names = {clean_name(e.get("summary", "")).lower() for e in deadlines}
    done_names = ledger.completed_names()

    def importable(project: dict) -> list[dict]:
        return [
            st
            for st in project["subtasks"]
            if not st["done"]
            and st["name"].lower() not in queue_names
            and st["name"].lower() not in done_names
        ]

    stored = ledger.get_state("active_project")
    active = None
    if project_override:
        needle = project_override.lower()
        matches = [p for p in projects if needle in p["name"].lower()]
        if not matches:
            available = ", ".join(p["name"] for p in projects)
            return (f"No Belki project matching **{project_override}**. Available: {available}.", 0)
        active = matches[0]
    else:
        stored_project = next(
            (p for p in projects if stored and p["name"].lower() == stored.lower()),
            None,
        )
        if stored_project and any(
            st["name"].lower() in queue_names for st in stored_project["subtasks"]
        ):
            active = stored_project
        else:
            candidates = sorted(
                (p for p in projects if p["deadline"] and importable(p)),
                key=lambda p: p["deadline"],
            )
            if not candidates:
                if stored_project:
                    return (
                        f"✅ **{stored_project['name']}** is done and no other Belki project "
                        f"with a deadline has new subtasks. Nothing to import.",
                        0,
                    )
                return ("Nothing to import — no Belki project with a deadline has new subtasks.", 0)
            active = candidates[0]

    new_tasks = importable(active)
    if not new_tasks:
        return (f"Nothing to import — **{active['name']}** has no new subtasks.", 0)

    switched = stored is not None and stored.lower() != active["name"].lower()

    today = datetime.datetime.now(ledger.TORONTO_TZ).date()
    taken = {
        e["start"]["dateTime"][:10]
        for e in deadlines
        if e.get("start", {}).get("dateTime")
    }
    slot = _next_sunday_on_or_after(today + datetime.timedelta(days=1))

    lines = []
    if switched:
        deadline_note = f" (deadline {active['deadline']})" if active["deadline"] else ""
        lines.append(f"🔀 Active project switched to **{active['name']}**{deadline_note}.")
    lines.append(f"📥 Imported {len(new_tasks)} subtask(s) from **{active['name']}**:")

    imported = 0
    for st in new_tasks:
        while slot.isoformat() in taken:
            slot += datetime.timedelta(days=7)
        due = slot.isoformat()
        insert_deadline(
            f"{ledger.DEADLINE_PREFIX} {st['name']} — DUE",
            f"{due}T23:59:00",
            f"{due}T23:59:00",
            origin="belki_import",
            estimate=st["estimate"],
            body_text=st["description"] or None,
            project=active["name"],
        )
        taken.add(due)
        slot += datetime.timedelta(days=7)
        imported += 1
        est_note = (
            f" (est: {st['estimate']} evening{'s' if st['estimate'] != 1 else ''})"
            if st["estimate"] is not None
            else " (no estimate)"
        )
        lines.append(f"• {st['name']} — due {due}{est_note}")
        if st["estimate"] is not None and st["estimate"] > 7:
            lines.append(
                f"  ⚠️ estimated {st['estimate']} evenings (>1 week) — consider splitting it in Belki."
            )

    if active["skipped"]:
        lines.append("Skipped lines I couldn't parse:")
        lines.extend(f"  ✗ {s}" for s in active["skipped"][:5])

    if not dry_run:
        ledger.set_state("active_project", active["name"])
    return ("\n".join(lines), imported)
