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

Sync is bidirectional: a task marked `- [x]` in Belki since the last sync
is treated as completed and its ⏰ calendar event is deleted (via
complete_deadline), so finishing work in a Claude Code / Belki session is
enough — Discord never has to be told separately. This only reconciles the
currently (or previously) active project, matched by cleaned task name.

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
_META_ID_RE = re.compile(r"belki_id=(\S+)")
_META_BODY_RE = re.compile(r"Originally due: \d{4}-\d{2}-\d{2}\n\n(.*)", re.DOTALL)


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
    deadlines: list,
    insert_deadline,
    complete_deadline=None,
    update_deadline=None,
    project_override: str = None,
    dry_run: bool = False,
) -> tuple[str, int, int, int]:
    """Imports the active project's new tasks, packing weeks by estimate.
    Also completes any queued ⏰ event whose Belki task is now marked done,
    and reconciles renames/edits on tasks already tracked by belki_id.

    deadlines: current ⏰ queue events — should include overdue events too
    (main.get_tether_deadlines() + main.get_overdue_tether_events()) so a
    task finished late in Belki still gets cleared.
    insert_deadline: main._insert_deadline.
    complete_deadline: main.complete_task, or None to skip auto-completion
    (e.g. dry runs never pass one).
    update_deadline: main.update_deadline_content, or None to skip
    reconciliation (dry runs never pass one). Matching is by the belki_id
    stamped into TETHER_META at import time — a task renamed or re-described
    in Belki updates its existing event in place instead of leaving an
    orphaned event and importing a duplicate under the new name.
    dry_run: don't persist the active-project switch or complete anything
    (caller passes a non-inserting insert_deadline too).
    Returns (reply text, number imported, number auto-completed, number
    reconciled). Callers that only fire a notification on activity must
    check all three counts — a sync that only clears finished Belki tasks,
    or only reconciles a rename, has imported == 0.
    """
    if not os.path.isdir(BELKI_PATH):
        return (f"⚠️ Belki folder not found at `{BELKI_PATH}`.", 0, 0, 0)

    tasks, skipped = load_tasks()
    queue_by_name = {clean_name(e.get("summary", "")).lower(): e for e in deadlines}
    queue_names = set(queue_by_name)
    queue_by_id = {}
    for e in deadlines:
        m = _META_ID_RE.search(e.get("description", "") or "")
        if m:
            queue_by_id[m.group(1)] = e
    done_names = ledger.completed_names()
    stored = ledger.get_state("active_project")
    today = datetime.datetime.now(ledger.TORONTO_TZ).date()

    def is_fixed(t: dict) -> bool:
        return bool(t["due"]) and datetime.date.fromisoformat(t["due"]) > today

    completed_lines: list[str] = []
    if complete_deadline and not dry_run and stored:
        belki_done = {
            t["name"].lower()
            for t in tasks
            if t["project"]
            and t["project"].lower() == stored.lower()
            and t["done"]
        }
        for name_lower in belki_done & queue_names:
            event = queue_by_name[name_lower]
            complete_deadline(event["id"])
            completed_lines.append(
                f"✅ {clean_name(event.get('summary', ''))} — marked done in Belki, cleared."
            )

    def finish(text: str, imported: int, reconciled: int = 0) -> tuple[str, int, int, int]:
        if completed_lines:
            text = "\n".join(completed_lines) + "\n\n" + text
        return text, imported, len(completed_lines), reconciled

    counts = open_project_counts(tasks)
    if not counts:
        return finish("⚠️ No open Belki tasks with a `project::` found in `Data/*.md`.", 0)

    def importable(pname: str) -> list[dict]:
        return [
            t
            for t in tasks
            if t["project"]
            and t["project"].lower() == pname.lower()
            and not t["done"]
            and t["name"].lower() not in queue_names
            and t["name"].lower() not in done_names
            and not (t["id"] and t["id"] in queue_by_id)
        ]

    listing = ", ".join(f"**{p}** ({n} open)" for p, n in counts.items())
    if project_override:
        needle = project_override.lower()
        matches = [p for p in counts if needle in p.lower()]
        if not matches:
            return finish(f"No Belki project matching **{project_override}**. Available: {listing}.", 0)
        active = matches[0]
    else:
        active = next(
            (p for p in counts if stored and p.lower() == stored.lower()), None
        )
        if active is None:
            if stored:
                return finish(
                    f"✅ **{stored}** has no open Belki tasks left. Pick the next project "
                    f"with `@Tether sync belki <name>` — available: {listing}.",
                    0,
                )
            return finish(
                f"No active Belki project set. Pick one with `@Tether sync belki <name>` "
                f"— available: {listing}.",
                0,
            )

    # Any belki_id-tracked event whose task no longer exists anywhere in
    # Belki (line deleted outright, not marked `- [x]`) is left alone — the
    # signal is too ambiguous to auto-delete on — but surfaced for review.
    all_ids = {t["id"] for t in tasks if t["id"]}
    vanished = [
        clean_name(e.get("summary", ""))
        for bid, e in queue_by_id.items()
        if bid not in all_ids
    ]
    vanished_line = (
        f"⚠️ {len(vanished)} previously-imported task(s) no longer found in Belki "
        "(deleted, not marked done) — not auto-removed, review: " + ", ".join(vanished)
        if vanished
        else None
    )

    # Reconcile tasks already tracked by belki_id: a rename or a content/due
    # edit updates the existing event in place instead of leaving it orphaned
    # while a same-conceptual-task re-imports under its new name.
    reconciled_lines: list[str] = []
    if update_deadline and not dry_run:
        for t in tasks:
            if (
                not t["id"]
                or t["done"]
                or not t["project"]
                or t["project"].lower() != active.lower()
            ):
                continue
            event = queue_by_id.get(t["id"])
            if not event:
                continue
            cur_name = clean_name(event.get("summary", ""))
            cur_desc = event.get("description", "") or ""
            body_match = _META_BODY_RE.search(cur_desc)
            cur_body = body_match.group(1).strip() if body_match else ""
            cur_due = (event.get("start", {}) or {}).get("dateTime", "")[:10]
            fixed = is_fixed(t)
            want_due = t["due"] if fixed else cur_due
            name_changed = cur_name.lower() != t["name"].lower()
            desc_changed = cur_body != (t["description"] or "")
            due_changed = fixed and want_due != cur_due
            if not (name_changed or desc_changed or due_changed):
                continue
            new_summary = f"{ledger.DEADLINE_PREFIX} {t['name']} — DUE"
            update_deadline(event["id"], new_summary, want_due, t["description"] or None, t["estimate"])
            bits = []
            if name_changed:
                bits.append(f'renamed from "{cur_name}"')
            if desc_changed:
                bits.append("description updated")
            if due_changed:
                bits.append(f"due moved to {want_due}")
            reconciled_lines.append(f"🔄 {t['name']} — {', '.join(bits)}")

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
        pre_lines = reconciled_lines + ([vanished_line] if vanished_line else [])
        prefix = "\n".join(pre_lines) + "\n\n" if pre_lines else ""
        return finish(
            f"{prefix}Nothing to import — **{active}** has no new tasks.{hint}",
            0,
            len(reconciled_lines),
        )

    switched = stored is not None and stored.lower() != active.lower()

    usage = week_usage(deadlines)
    slot = _next_sunday_on_or_after(today + datetime.timedelta(days=1))

    for t in new_tasks:
        if is_fixed(t):
            week = _next_sunday_on_or_after(
                datetime.date.fromisoformat(t["due"])
            ).isoformat()
            usage[week] = usage.get(week, 0) + _need(t)

    lines = []
    if switched:
        lines.append(f"🔀 Active project switched to **{active}**.")
    if reconciled_lines:
        lines.extend(reconciled_lines)
        lines.append("")
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
            belki_id=t["id"],
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

    if vanished_line:
        lines.append(vanished_line)

    if skipped:
        lines.append("Skipped lines I couldn't parse:")
        lines.extend(f"  ✗ {s}" for s in skipped[:5])

    if not dry_run:
        ledger.set_state("active_project", active)
    return finish("\n".join(lines), imported, len(reconciled_lines))
