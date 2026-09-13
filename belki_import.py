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
      depends_on:: task-yyyyyyyy-yyyyyy
      status:: parked
      description:: one line

Only tasks carrying a project:: are importable — unassigned tasks are
invisible to Tether. estimate:: is integer evenings and drives capacity
packing (EVENINGS_PER_WEEK per week); a task without an estimate
conservatively fills its whole week. A task with a future due:: keeps that
exact date instead of being packed; a past due:: is repacked.

depends_on:: names one other task's id:: as a prerequisite. A task with an
open (not `- [x]`) dependency is invisible to both the weekly evening
allocator and the importer — it never gets scheduled, and its evenings
aren't counted as spendable this week — until the dependency is checked off.
A depends_on:: naming itself or an id that doesn't exist anywhere in Belki is
ignored (surfaced as a parser note), not treated as blocking forever. No
transitive chains: only the one named task is checked, so A blocking on B
blocking on C does not make A wait on C. If depends_on:: is added to a task
*after* it was already imported (already has a ⏰ event), sync() clears that
event too — the same delete_deadline path status:: parked uses — since
otherwise the new dependency would silently do nothing until the event
cleared some other way.

status:: parked marks a task Simon has deliberately abandoned but not done —
the "dismiss without lying that it's complete" case `complete` can't cover
without corrupting ledger.velocity(). Like a blocked task, a parked one is
excluded from evening allocation and import; unlike a blocked one, if it
already has a tracked ⏰ event that event is deleted outright (recorded as
`deleted` in the ledger, same path as delete_calendar_event, so it never
counts toward velocity()). Removing status:: parked (or checking the task
`- [x]`) picks it back up on the next sync like any other task. This is
unrelated to projects.md's own status:: field (project-level active/paused)
— same field name, different card type, different scope.

Several projects run at once. Each week's EVENINGS_PER_WEEK evenings are
split across them by deadline pressure, read from a registry at
BELKI_PATH/projects.md:

    - Tiffy's Classroom
      due:: 2026-11-01
      status:: active

A project's required rate is its open evenings over the weeks left until
that date; the week's evenings are apportioned by that rate, capped at what
each project actually has left, with the remainder going to undeadlined
projects longest-untouched-first. The split is computed once and pinned for
the week (state key 'allocation:<sunday>'), so finishing early leaves the
week clear instead of pulling the next task into the gap.

Forecasting uses measured output — ledger.velocity() — not the sum of open
estimates. Tasks churn every session, so a burndown never converges and
every project reads as doomed; the deadline sets the stakes and the
completion history sets the rate.

Sync is bidirectional: a task marked `- [x]` in Belki since the last sync
is treated as completed and its ⏰ calendar event is deleted (via
complete_deadline), so finishing work in a Claude Code / Belki session is
enough — Discord never has to be told separately. This covers every project
in the queue, matched by cleaned task name.

Parsing is lenient: unparseable lines are reported in the sync reply, never
fatal. main.py injects its calendar functions into sync() — this module
never talks to Google directly.
"""

import datetime
import json
import os
import re

import ledger
from ledger import clean_name

BELKI_PATH = os.environ.get("BELKI_PATH", "/app/belki")
EVENINGS_PER_WEEK = int(os.environ.get("EVENINGS_PER_WEEK", "6"))

# Health of a sync, so callers can tell a benign zero ("nothing new to
# import") from a degraded one ("can't see Belki at all"). Both return
# imported/completed/reconciled == 0, and for a week after the Oracle VM
# cutover that ambiguity hid a dead sync completely: the ledger's
# active_project was never carried over, every cycle bailed out at
# STATUS_NO_ACTIVE_PROJECT, and nothing was ever said.
STATUS_OK = "ok"
STATUS_NO_BELKI_DIR = "no_belki_dir"
STATUS_NO_TASKS = "no_tasks"
STATUS_NO_ACTIVE_PROJECT = "no_active_project"
# The completion pass deletes calendar events *before* the rest of sync() runs;
# if imports/reconciles then raise, the "✅ cleared" lines used to die with the
# stack frame (every caller just logs the exception) — event gone, nag stopped,
# nothing said. sync() now catches that, returns what it managed, and marks the
# result with this so the caller still speaks up (see the outer try in sync()).
STATUS_SYNC_ERROR = "sync_error"

DEGRADED_STATUSES = (
    STATUS_NO_BELKI_DIR,
    STATUS_NO_TASKS,
    STATUS_NO_ACTIVE_PROJECT,
    STATUS_SYNC_ERROR,
)

_CHECKBOX_RE = re.compile(r"^- \[( |x|X)\] (.+)$")
# Registry bullet: `- Project Name`, deliberately NOT a checkbox, so a task
# line can never be mistaken for a project entry.
_PROJECT_LINE_RE = re.compile(r"^- (?!\[)(.+)$")
_FIELD_RE = re.compile(r"^\s+([A-Za-z_]+)::\s*(.*)$")
# Wider than _FIELD_RE (digits, hyphens) — used only to *notice* an indented
# `key:: value` line the real parser would skip, e.g. a typo'd `deadline::`
# or `due-date::` in place of `due::`. Detection only; nothing is parsed off it.
_ANYKEY_RE = re.compile(r"^\s+([A-Za-z0-9_-]+)::(?:\s|$)")
# Keys the task-card parser understands (parsed) or deliberately leaves to
# Belki (created/labels/completed are written by `/update`). Anything else
# indented under an OPEN task is surfaced as a parser note rather than
# silently dropped — an edit naming an unknown key looks applied but does
# nothing (Simon lost time to `deadline:: 2026-09-12` on 2026-09-01).
_KNOWN_TASK_KEYS = frozenset(
    {"estimate", "est", "due", "project", "description", "id", "priority",
     "created", "labels", "completed", "depends_on", "status"}
)
_META_EST_RE = re.compile(r"estimate=(\d+)")
_META_ID_RE = re.compile(r"belki_id=(\S+)")
_META_PROJECT_RE = re.compile(r"^project=(.+)$", re.M)
_META_PRIORITY_RE = re.compile(r"^priority=(.+)$", re.M)
_META_BODY_RE = re.compile(r"Originally due: \d{4}-\d{2}-\d{2}\n\n(.*)", re.DOTALL)


def _unknown_key_note(task_name: str, key: str) -> str:
    hint = (
        " — did you mean `due::`?"
        if key in ("deadline", "due-date", "duedate", "by", "deadline-date")
        else ""
    )
    return f'"{task_name[:60]}": unrecognised field `{key}::` ignored{hint}'


def parse_data_file(path: str) -> tuple[list[dict], list[str], list[str]]:
    """Parses one monthly Belki data file.

    Returns (tasks, skipped, notes). `skipped` is lines the parser could not
    read; `notes` is lines it read fine but chose to ignore (an unrecognised
    `key::` under an open task) — kept separate so "couldn't parse" stays
    honest. Each task: {name, id, project, estimate, due, description, done,
    priority}.
    """
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()

    tasks: list[dict] = []
    skipped: list[str] = []
    notes: list[str] = []
    current: dict | None = None
    last_field_key: str | None = None  # for spotting description:: continuations

    for line in lines:
        match = _CHECKBOX_RE.match(line)
        if match:
            last_field_key = None
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
                "depends_on": None,
                "status": None,
            }
            tasks.append(current)
            continue
        field = _FIELD_RE.match(line)
        if field and current:
            key, val = field.group(1).lower(), field.group(2).strip()
            # A `word:: ...` line right after description:: is almost always a
            # wrapped continuation of that prose, not a new field — don't flag
            # an unknown key on it (known keys still parse), and keep the
            # description context so a multi-line wrap stays covered.
            in_desc_wrap = last_field_key == "description"
            if key in _KNOWN_TASK_KEYS or not in_desc_wrap:
                last_field_key = key
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
            elif key == "depends_on":
                current["depends_on"] = val
            elif key == "status":
                current["status"] = val.lower()
            elif key not in _KNOWN_TASK_KEYS and not current["done"] and not in_desc_wrap:
                notes.append(_unknown_key_note(current["name"], key))
            continue
        anykey = _ANYKEY_RE.match(line)
        if anykey and current and not current["done"] and last_field_key != "description":
            # An indented `key:: value` _FIELD_RE wouldn't match (digit or
            # hyphen in the key). Still a field the writer meant to set —
            # unless we're inside a wrapped description, where it's just prose.
            key = anykey.group(1).lower()
            if key not in _KNOWN_TASK_KEYS:
                notes.append(_unknown_key_note(current["name"], key))
            continue
        if line.startswith("- ") and line.strip():
            # top-level list line that isn't a checkbox
            skipped.append(line.strip())
            current = None
            last_field_key = None
            continue
        if line.strip() and line[:1] not in (" ", "\t", "#"):
            current = None
            last_field_key = None

    return tasks, skipped, notes


def load_tasks(belki_path: str = None) -> tuple[list[dict], list[str], list[str]]:
    """All tasks from BELKI_PATH/Data/*.md in file order. Duplicate ids keep
    the first occurrence. Returns (tasks, skipped, notes) — see
    parse_data_file for the skipped/notes split."""
    data_dir = os.path.join(belki_path or BELKI_PATH, "Data")
    if not os.path.isdir(data_dir):
        return [], [f"(no Data folder at {data_dir})"], []
    tasks: list[dict] = []
    skipped: list[str] = []
    notes: list[str] = []
    seen_ids: set[str] = set()
    for fname in sorted(os.listdir(data_dir)):
        if not fname.lower().endswith(".md"):
            continue
        try:
            file_tasks, file_skipped, file_notes = parse_data_file(
                os.path.join(data_dir, fname)
            )
        except Exception as e:
            skipped.append(f"({fname} unreadable: {e})")
            continue
        skipped.extend(file_skipped)
        notes.extend(file_notes)
        for t in file_tasks:
            if t["id"] and t["id"] in seen_ids:
                continue
            if t["id"]:
                seen_ids.add(t["id"])
            tasks.append(t)
    return tasks, skipped, notes


def annotate_dependencies(tasks: list[dict]) -> list[str]:
    """Resolves each task's depends_on:: against the full task list, in place.

    Sets t["blocked_by"] to the prerequisite task's dict while it's still
    open, or None once satisfied/absent. A depends_on:: naming itself or an
    id absent from every Data/*.md file is cleared and reported as a note —
    it can never be satisfied, so treating it as blocking would strand the
    task forever over what's almost certainly a typo. Single-hop only: the
    prerequisite's own depends_on:: is never followed, so a dependency cycle
    (A on B, B on A) blocks both permanently rather than looping — an
    accepted limitation, not detected or fixed here.
    """
    by_id = {t["id"]: t for t in tasks if t["id"]}
    notes: list[str] = []
    for t in tasks:
        dep = t["depends_on"]
        if not dep or t["done"]:
            t["blocked_by"] = None
            continue
        if dep == t["id"]:
            notes.append(f'"{t["name"][:60]}": depends_on:: references itself, ignored')
            t["depends_on"] = None
            t["blocked_by"] = None
            continue
        parent = by_id.get(dep)
        if parent is None:
            notes.append(f'"{t["name"][:60]}": depends_on:: {dep} not found, ignored')
            t["depends_on"] = None
            t["blocked_by"] = None
            continue
        t["blocked_by"] = parent if not parent["done"] else None
    return notes


def open_project_counts(tasks: list[dict]) -> dict:
    """Open-task counts per project name, in first-seen order."""
    counts: dict[str, int] = {}
    for t in tasks:
        if t["project"] and not t["done"]:
            counts[t["project"]] = counts.get(t["project"], 0) + 1
    return counts


def parse_projects(path: str) -> dict:
    """Parses the project registry at BELKI_PATH/projects.md.

        - Tiffy's Classroom
          due:: 2026-11-01
          status:: active

    Returns {name: {"due": date|None, "status": str}}. `due:: none` and a
    missing due are both None — a project without a deadline still gets
    worked on, it just can't generate deadline pressure.
    """
    out: dict[str, dict] = {}
    has_fields: set[str] = set()
    current: str | None = None
    with open(path, "r", encoding="utf-8") as f:
        for line in f.read().splitlines():
            field = _FIELD_RE.match(line)
            if field and current:
                key, val = field.group(1).lower(), field.group(2).strip()
                if key == "due":
                    has_fields.add(current)
                    try:
                        out[current]["due"] = datetime.date.fromisoformat(val)
                    except ValueError:
                        out[current]["due"] = None  # "none", "tbd", junk
                elif key == "status":
                    has_fields.add(current)
                    out[current]["status"] = val.lower()
                continue
            bullet = _PROJECT_LINE_RE.match(line)
            if bullet:
                current = bullet.group(1).strip()
                out.setdefault(current, {"due": None, "status": "active"})
                continue
            if line.strip() and line[:1] not in (" ", "\t", "#"):
                current = None
    # An entry only counts if it actually carried due::/status::. This file is
    # hand-edited and its header explains the fields using ordinary markdown
    # bullets — without this, "- `due:: none` — no deadline..." registers a
    # project literally named "`due:: none` — no deadline...".
    return {k: v for k, v in out.items() if k in has_fields}


def load_projects(belki_path: str = None) -> tuple[dict, str | None]:
    """Registry + an optional note explaining why it's empty."""
    path = os.path.join(belki_path or BELKI_PATH, "projects.md")
    if not os.path.isfile(path):
        return {}, f"(no projects.md at {path} — every project treated as active, no deadlines)"
    try:
        return parse_projects(path), None
    except Exception as e:
        return {}, f"(projects.md unreadable: {e})"


def _registry(projects: dict, name: str) -> dict | None:
    for k, v in projects.items():
        if k.lower() == name.lower():
            return v
    return None


def _largest_remainder(weights: dict, total: int) -> dict:
    """Apportion `total` whole evenings across `weights` (Hamilton method).

    Ties break by name so the same inputs always produce the same split —
    an allocation that quietly reshuffles between syncs is unusable.
    """
    s = sum(weights.values())
    if s <= 0 or total <= 0:
        return {k: 0 for k in weights}
    exact = {k: total * w / s for k, w in weights.items()}
    out = {k: int(v) for k, v in exact.items()}
    order = sorted(weights, key=lambda k: (-(exact[k] - out[k]), k))
    for k in order[: total - sum(out.values())]:
        out[k] += 1
    return out


def _neglect_order(names: list, last_activity) -> list:
    """Longest-untouched first; never-touched first of all."""

    def key(n):
        d = last_activity(n) if last_activity else None
        return (0 if d is None else 1, -(d or 0), n)

    return sorted(names, key=key)


def allocate_evenings(
    projects: dict,
    tasks: list,
    today: datetime.date,
    capacity: int = None,
    last_activity=None,
    min_units: dict = None,
) -> tuple[dict, list]:
    """Splits the coming week's evenings across projects by deadline pressure.

    projects: registry from load_projects(). Advisory — a project with open
    tasks but no registry entry is treated as active with no deadline, so
    forgetting to register something never makes its work invisible.
    last_activity: fn(project) -> days since that project last moved, or None
    if it never has. Only orders projects that have no deadline to compete on.

    Returns (allocation, lines). The lines explain the split and are meant to
    be shown, not logged: pressure you can't interrogate is pressure you stop
    believing.
    """
    capacity = EVENINGS_PER_WEEK if capacity is None else capacity
    open_by_project: dict[str, list] = {}
    for t in tasks:
        if t["project"] and not t["done"]:
            open_by_project.setdefault(t["project"], []).append(t)

    eligible: dict[str, dict] = {}
    for name, ts in open_by_project.items():
        entry = _registry(projects, name)
        if entry and entry.get("status", "active") != "active":
            continue
        eligible[name] = {
            # Blocked (open depends_on::) and parked (status:: parked) tasks
            # don't count toward what a project can spend this week —
            # otherwise a project whose next tasks are all waiting or
            # abandoned still gets allocated evenings for them and can't
            # spend any of it.
            "remaining": sum(
                _need(t) for t in ts
                if not t.get("blocked_by") and t.get("status") != "parked"
            ),
            "due": (entry or {}).get("due"),
        }
    if not eligible:
        return {}, []

    lines: list[str] = []
    required: dict[str, float] = {}
    overdue: list[str] = []
    for name, info in eligible.items():
        if not info["due"]:
            required[name] = 0.0
            continue
        days_left = (info["due"] - today).days
        if days_left <= 0:
            overdue.append(name)
        # A past-due project doesn't get infinite pressure, it gets one
        # week's worth — enough to dominate, not enough to divide by zero.
        required[name] = info["remaining"] / (max(days_left, 1) / 7.0)

    pressured = {k: v for k, v in required.items() if v > 0}
    alloc = _largest_remainder(pressured, capacity) if pressured else {}
    for k in list(alloc):
        alloc[k] = min(alloc[k], eligible[k]["remaining"])

    # Spare evenings go to whoever can still use them: deadline projects by
    # pressure, then undeadlined ones by neglect.
    order = sorted(pressured, key=lambda k: (-required[k], k)) + _neglect_order(
        [k for k in eligible if k not in pressured], last_activity
    )
    # Round-robin, one evening at a time, rather than filling each project in
    # turn. Filling greedily meant that with no deadlines set anywhere — the
    # state this ships in — the alphabetically-first project swallowed the
    # entire week and nothing else was ever scheduled.
    leftover = capacity - sum(alloc.values())
    while leftover > 0:
        progressed = False
        for k in order:
            if leftover <= 0:
                break
            if alloc.get(k, 0) < eligible[k]["remaining"]:
                alloc[k] = alloc.get(k, 0) + 1
                leftover -= 1
                progressed = True
        if not progressed:
            break  # everyone is at their remaining work; the week isn't full
    alloc = {k: v for k, v in alloc.items() if v > 0}

    # A slice smaller than the project's next task cannot be spent. Left
    # as-is that project imports nothing, every week, forever — its share is
    # allocated and then silently evaporates. Round each slice up to a
    # spendable size in share order until the week runs out; whoever no
    # longer fits waits for a week where they do.
    if min_units:
        rebuilt: dict[str, int] = {}
        budget = capacity
        starved: list[str] = []
        for k in sorted(alloc, key=lambda k: (-alloc[k], -required.get(k, 0), k)):
            unit = min_units.get(k) or 1
            take = min(max(alloc[k], unit), eligible[k]["remaining"], budget)
            if take >= unit:
                rebuilt[k] = take
                budget -= take
            else:
                starved.append(f"{k} (next task needs {unit} ev)")
        if starved:
            lines.append(
                "⏸ No room this week for: " + ", ".join(sorted(starved)) + "."
            )
        # Evenings freed by a project that couldn't use them go back to the
        # projects that can, rather than sitting idle for the week.
        while budget > 0 and rebuilt:
            progressed = False
            for k in sorted(rebuilt, key=lambda k: (-required.get(k, 0), k)):
                if budget <= 0:
                    break
                if rebuilt[k] < eligible[k]["remaining"]:
                    rebuilt[k] += 1
                    budget -= 1
                    progressed = True
            if not progressed:
                break
        alloc = rebuilt

    committed = sum(required.values())
    if committed > capacity:
        lines.append(
            f"⚠️ Deadlines commit you to **{committed:.1f} evenings/week** against a "
            f"capacity of {capacity}. Something slips — move a date in `projects.md` "
            f"or cut scope."
        )
    if overdue:
        lines.append(f"🔴 Past its project deadline: {', '.join(sorted(overdue))}.")
    for name in sorted(alloc, key=lambda k: (-alloc[k], k)):
        info = eligible[name]
        due_note = f" · due {info['due']}" if info["due"] else ""
        lines.append(
            f"• **{name}** — {alloc[name]} ev this week, {info['remaining']} left{due_note}"
        )
    return alloc, lines


def project_report(today: datetime.date = None) -> list:
    """Per-project deadline vs measured rate, for the weekly summary.

    Shows the required rate next to the observed one. A project is AT RISK
    when it is producing slower than its deadline demands — which is a
    statement about output, not about how many cards are open, so adding
    tasks doesn't move it and finishing them does.
    """
    if not os.path.isdir(BELKI_PATH):
        return []
    tasks, _, _ = load_tasks()
    projects, _note = load_projects()
    today = today or datetime.datetime.now(ledger.TORONTO_TZ).date()

    remaining: dict[str, int] = {}
    for t in tasks:
        if t["project"] and not t["done"]:
            remaining[t["project"]] = remaining.get(t["project"], 0) + _need(t)
    if not remaining:
        return []

    lines = ["📊 **Projects**"]
    for name in sorted(remaining):
        entry = _registry(projects, name)
        if entry and entry.get("status", "active") != "active":
            continue
        due = (entry or {}).get("due")
        if not due:
            lines.append(f"• **{name}** — {remaining[name]} ev open")
            continue
        days_left = (due - today).days
        need = remaining[name] / (max(days_left, 1) / 7.0)
        observed = ledger.velocity(name)
        flag = ""
        if days_left <= 0:
            flag = "  🔴 PAST DEADLINE"
        elif observed < need:
            flag = "  ⚠️ AT RISK"
        lines.append(f"• **{name}** — {remaining[name]} ev open · due {due}{flag}")
    return lines if len(lines) > 1 else []


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


def _project_week_usage(deadlines: list, project: str, week: str) -> int:
    """Evenings a project already holds in one week.

    Events predating the project= stamp aren't attributable and count as
    nobody's — they still consume the week's total through week_usage(), they
    just can't be charged against a project's budget until the reconcile pass
    tags them.
    """
    used = 0
    for e in deadlines:
        due = (e.get("start", {}) or {}).get("dateTime", "")[:10]
        if not due:
            continue
        try:
            if _next_sunday_on_or_after(datetime.date.fromisoformat(due)).isoformat() != week:
                continue
        except ValueError:
            continue
        desc = e.get("description", "") or ""
        m = _META_PROJECT_RE.search(desc)
        if not m or m.group(1).strip().lower() != project.lower():
            continue
        est = _META_EST_RE.search(desc)
        used += int(est.group(1)) if est else EVENINGS_PER_WEEK
    return used


def _week_key(week: datetime.date) -> str:
    return f"allocation:{week.isoformat()}"


def _week_allocation(
    projects: dict,
    tasks: list,
    today: datetime.date,
    week: datetime.date,
    dry_run: bool,
    deadlines: list,
    min_units: dict = None,
) -> tuple[dict, dict, list]:
    """This week's evening split — computed once, then pinned for the week.

    Returns (allocation, spent, lines).

    Pinning is the point. Recomputing on every 30-minute sync would mean
    finishing a task immediately pulls the next one into the same week, so
    working faster just refills the slot and the deadline never moves — the
    exact treadmill that made the old per-task due dates meaningless.

    `spent` has to be *recorded* rather than measured off the live queue for
    the same reason: completing a task deletes its event, so a queue-derived
    figure drops and the freed evening is immediately handed to the next task.
    Spend is a fact about the week, not about what's currently on the
    calendar. Finishing early leaves the week genuinely clear.

    A new week — or an explicit `sync belki <name>` — recomputes.
    """
    key = _week_key(week)
    stored = ledger.get_state(key)
    if stored:
        try:
            pinned = json.loads(stored)
            if isinstance(pinned, dict) and pinned.get("alloc"):
                return pinned["alloc"], dict(pinned.get("spent") or {}), []
        except ValueError:
            pass  # corrupt pin, recompute
    alloc, lines = allocate_evenings(
        projects,
        tasks,
        today,
        last_activity=ledger.days_since_activity,
        min_units=min_units,
    )
    # Seed spend from whatever this project already holds in the target week,
    # so a mid-week first run tops up instead of double-booking.
    week_iso = week.isoformat()
    spent = {p: _project_week_usage(deadlines, p, week_iso) for p in alloc}
    if alloc and not dry_run:
        ledger.set_state(key, json.dumps({"alloc": alloc, "spent": spent}))
    return alloc, spent, lines


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
    delete_deadline=None,
    project_override: str = None,
    dry_run: bool = False,
) -> tuple[str, int, int, int, str]:
    """Schedules the coming week's work across projects by deadline pressure.
    Also completes any queued ⏰ event whose Belki task is now marked done,
    clears any queued ⏰ event whose Belki task is now marked status:: parked
    or newly blocked by an open depends_on::, and reconciles renames/edits on
    tasks already tracked by belki_id.

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
    delete_deadline: main.delete_calendar_event, or None to skip auto-clearing
    of parked or newly-blocked tasks (e.g. dry runs never pass one). Unlike
    complete_deadline, this records `deleted` in the ledger (via
    delete_calendar_event itself), not `completed` — neither parking nor
    blocking a task may ever count toward velocity().
    project_override: an explicit `sync belki <name>` — hands the whole week
    to that project and replaces whatever was pinned.
    dry_run: don't pin the allocation or complete anything (caller passes a
    non-inserting insert_deadline too).
    Returns (reply text, number imported, number auto-completed or parked-
    cleared, number reconciled, status). Callers that only fire a
    notification on activity must check all three counts — a sync that only
    clears finished or parked Belki tasks, or only reconciles a rename, has
    imported == 0 — and must check status too, or a sync that is failing
    outright looks identical to a quiet one (see the STATUS_* constants).
    """
    if not os.path.isdir(BELKI_PATH):
        return (
            f"⚠️ Belki folder not found at `{BELKI_PATH}`.",
            0,
            0,
            0,
            STATUS_NO_BELKI_DIR,
        )

    tasks, skipped, notes = load_tasks()
    notes = notes + annotate_dependencies(tasks)
    queue_by_name = {clean_name(e.get("summary", "")).lower(): e for e in deadlines}
    queue_names = set(queue_by_name)
    queue_by_id = {}
    for e in deadlines:
        m = _META_ID_RE.search(e.get("description", "") or "")
        if m:
            queue_by_id[m.group(1)] = e
    done_names = ledger.completed_names()
    projects, registry_note = load_projects()
    today = datetime.datetime.now(ledger.TORONTO_TZ).date()

    def is_fixed(t: dict) -> bool:
        return bool(t["due"]) and datetime.date.fromisoformat(t["due"]) > today

    completed_lines: list[str] = []
    # Non-fatal problems (a single Calendar write that failed) — surfaced in the
    # reply and enough on their own to make finish() report a degraded status,
    # so a sync that only half-worked never looks like a clean no-op.
    sync_errors: list[str] = []
    if complete_deadline and not dry_run:
        # Every project, not just one: the queue now spans several at once, and
        # scoping this to the single active project meant a task finished in
        # Belki for any other project was never cleared from the calendar.
        belki_done = {
            t["name"].lower() for t in tasks if t["project"] and t["done"]
        }
        # Sorted so a mid-loop failure is deterministic; per-item try so one
        # bad delete doesn't strand the events already cleared this pass with
        # no notification (the whole point of the fix — see STATUS_SYNC_ERROR).
        for name_lower in sorted(belki_done & queue_names):
            event = queue_by_name[name_lower]
            title = clean_name(event.get("summary", ""))
            try:
                complete_deadline(event["id"])
            except Exception as e:
                sync_errors.append(
                    f'"{title}" is done in Belki but its event would not clear '
                    f"({type(e).__name__}) — check the calendar"
                )
                continue
            completed_lines.append(
                f"✅ {title} — marked done in Belki, cleared."
            )

    if delete_deadline and not dry_run:
        # A task parked mid-flight (already imported, now marked
        # status:: parked without being checked off) needs its event gone
        # too, or the nag it was set to silence just keeps firing off the
        # stale ⏰ event. `not t["done"]` keeps this disjoint from the
        # completion loop above — a task can't be both.
        belki_parked = {
            t["name"].lower()
            for t in tasks
            if t["project"] and not t["done"] and t.get("status") == "parked"
        }
        for name_lower in sorted(belki_parked & queue_names):
            event = queue_by_name[name_lower]
            title = clean_name(event.get("summary", ""))
            try:
                delete_deadline(event["id"])
            except Exception as e:
                sync_errors.append(
                    f'"{title}" is parked in Belki but its event would not clear '
                    f"({type(e).__name__}) — check the calendar"
                )
                continue
            completed_lines.append(
                f"⏸️ {title} — parked in Belki, cleared from calendar."
            )

        # Same problem, different cause: a task that gets a depends_on::
        # added *after* it was already imported is just as stuck-on-the-
        # calendar as a parked one — importable()/allocate_evenings() only
        # ever stop a *new* import, they never reach back and un-schedule
        # an event that already exists. Without this, adding depends_on::
        # to an in-flight task silently does nothing until that event
        # happens to clear some other way. Excludes parked tasks — already
        # handled above, and deleting the same event twice would just log a
        # spurious error on the second attempt.
        blocked_map = {
            t["name"].lower(): t
            for t in tasks
            if t["project"]
            and not t["done"]
            and t.get("blocked_by")
            and t.get("status") != "parked"
        }
        for name_lower in sorted(set(blocked_map) & queue_names):
            t = blocked_map[name_lower]
            event = queue_by_name[name_lower]
            title = clean_name(event.get("summary", ""))
            try:
                delete_deadline(event["id"])
            except Exception as e:
                sync_errors.append(
                    f'"{title}" is now blocked in Belki but its event would not '
                    f"clear ({type(e).__name__}) — check the calendar"
                )
                continue
            completed_lines.append(
                f'⛔ {title} — now waiting on "{t["blocked_by"]["name"]}", '
                f"cleared from calendar."
            )

    def finish(
        text: str, imported: int, reconciled: int = 0, status: str = STATUS_OK
    ) -> tuple[str, int, int, int, str]:
        parts = []
        if completed_lines:
            parts.append("\n".join(completed_lines))
        if sync_errors:
            parts.append(
                "⚠️ Sync problems (the rest still went through; will retry next cycle):\n"
                + "\n".join(f"  • {e}" for e in sync_errors)
            )
            if status == STATUS_OK:
                status = STATUS_SYNC_ERROR
        parts.append(text)
        return "\n\n".join(parts), imported, len(completed_lines), reconciled, status

    try:
        counts = open_project_counts(tasks)
        if not counts:
            # Surface `skipped` here: when the folder is mounted but empty (or the
            # Data/ dir is missing entirely) that list holds the only clue why,
            # and discarding it made this state undiagnosable from Discord alone.
            detail = (
                f" Parser notes: {'; '.join((skipped + notes)[:5])}"
                if (skipped or notes)
                else ""
            )
            return finish(
                "⚠️ No open Belki tasks with a `project::` found in `Data/*.md`." + detail,
                0,
                status=STATUS_NO_TASKS,
            )

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
                and not t.get("blocked_by")
                and t.get("status") != "parked"
            ]

        # Reported once, up front, so a depends_on::-blocked or parked task
        # doesn't just silently vanish from the sync reply — it's still an
        # open Belki task, it's just not this week's to schedule.
        blocked_lines = [
            f'⛔ [{t["project"]}] {t["name"]} — waiting on "{t["blocked_by"]["name"]}"'
            for t in tasks
            if t["project"]
            and not t["done"]
            and t["name"].lower() not in queue_names
            and t["name"].lower() not in done_names
            and t.get("blocked_by")
        ] + [
            f'🅿️ [{t["project"]}] {t["name"]} — parked'
            for t in tasks
            if t["project"]
            and not t["done"]
            and t["name"].lower() not in queue_names
            and t["name"].lower() not in done_names
            and t.get("status") == "parked"
        ]

        listing = ", ".join(f"**{p}** ({n} open)" for p, n in counts.items())
        week = _next_sunday_on_or_after(today + datetime.timedelta(days=1))

        if project_override:
            needle = project_override.lower()
            matches = [p for p in counts if needle in p.lower()]
            if not matches:
                return finish(f"No Belki project matching **{project_override}**. Available: {listing}.", 0)
            # Explicit override: the whole week goes to that project, and it
            # replaces whatever was pinned. Asking for a project by name is an
            # instruction, not a hint.
            alloc = {matches[0]: EVENINGS_PER_WEEK}
            spent = {matches[0]: 0}
            alloc_lines = [
                f"🎯 Override — the week's {EVENINGS_PER_WEEK} evenings go to **{matches[0]}**."
            ]
            if not dry_run:
                ledger.set_state(
                    _week_key(week), json.dumps({"alloc": alloc, "spent": spent})
                )
        else:
            # Size of each project's next importable task, so the allocator never
            # hands out a slice too small to schedule anything with.
            min_units = {}
            for pname in counts:
                nxt = importable(pname)
                if nxt:
                    min_units[pname] = _need(nxt[0])
            alloc, spent, alloc_lines = _week_allocation(
                projects, tasks, today, week, dry_run, deadlines, min_units=min_units
            )
        if registry_note and alloc_lines:
            alloc_lines.append(registry_note)

        if not alloc:
            detail = ("\n" + "\n".join(blocked_lines)) if blocked_lines else ""
            return finish(
                f"No project has open Belki tasks to schedule. Available: {listing}.{detail}",
                0,
                status=STATUS_NO_ACTIVE_PROJECT,
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

        # Backfill: events created before belki_id tracking existed (pre-2026-07-07)
        # carry no belki_id, so the lookup above can never find them — any future
        # rename permanently orphans them (old event stays stale, a duplicate
        # imports under the new name) instead of updating in place. If a legacy
        # event's title still matches a tracked task's current name, stamp the id
        # (and estimate) onto it now, before it has a chance to drift. Already-
        # orphaned events (title already changed) can't be recovered this way —
        # their old title no longer matches anything and needs manual cleanup.
        reconciled_lines: list[str] = []
        if update_deadline and not dry_run:
            for t in tasks:
                # No project filter: the queue spans every project now, so what's
                # in the queue defines the scope. Filtering to one project meant an
                # event belonging to any other could never be repaired.
                if not t["id"] or t["done"] or not t["project"] or t["id"] in queue_by_id:
                    continue
                legacy_event = queue_by_name.get(clean_name(t["name"]).lower())
                if not legacy_event or _META_ID_RE.search(legacy_event.get("description", "") or ""):
                    continue
                # Register only — don't write here. The reconcile pass below is
                # about to touch this same event and stamps belki_id itself, so
                # writing now just means two Calendar writes and a doubled
                # 🔧/🔄 pair in the reply for one logical change. Its `needs_id`
                # check is what guarantees the stamp still happens when nothing
                # else about the task differs.
                queue_by_id[t["id"]] = legacy_event

        # Reconcile tasks already tracked by belki_id: a rename or a content/due
        # edit updates the existing event in place instead of leaving it orphaned
        # while a same-conceptual-task re-imports under its new name.
        spent_dirty = False
        if update_deadline and not dry_run:
            for t in tasks:
                if not t["id"] or t["done"] or not t["project"]:
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
                # A due:: added/moved out mid-week pulls the task out of the
                # week it was pinned into — free the evenings it was holding
                # so the rest of the week isn't short until Sunday's recompute
                # (task-spentdrp-7k2n9q).
                if due_changed and cur_due:
                    old_week = _next_sunday_on_or_after(datetime.date.fromisoformat(cur_due))
                    new_week = _next_sunday_on_or_after(datetime.date.fromisoformat(want_due))
                    if old_week == week and new_week != week:
                        spent_key = next(
                            (p for p in spent if p.lower() == t["project"].lower()), None
                        )
                        if spent_key:
                            spent[spent_key] = max(0, spent[spent_key] - _need(t))
                            spent_dirty = True
                cur_priority_match = _META_PRIORITY_RE.search(cur_desc)
                cur_priority = cur_priority_match.group(1).strip() if cur_priority_match else None
                priority_changed = bool(t["priority"]) and t["priority"] != cur_priority
                # A legacy event registered by the backfill pass above carries no
                # belki_id yet. Stamp it even when nothing else differs — that is
                # the whole point of the backfill, and without this the id would
                # only ever land on a task that happened to change in the same
                # sync.
                needs_id = not _META_ID_RE.search(cur_desc)
                # Same idea for project=: events predating the multi-project queue
                # don't say which project they belong to, so they can't be grouped
                # or credited to a project's velocity until this stamps them.
                needs_project = not _META_PROJECT_RE.search(cur_desc)
                if not (
                    name_changed or desc_changed or due_changed or needs_id or needs_project
                    or priority_changed
                ):
                    continue
                new_summary = f"{ledger.DEADLINE_PREFIX} {t['name']} — DUE"
                try:
                    update_deadline(
                        event["id"], new_summary, want_due, t["description"] or None, t["estimate"],
                        belki_id=t["id"], project=t["project"], priority=t["priority"],
                    )
                except Exception as e:
                    sync_errors.append(
                        f'could not reconcile "{t["name"]}" ({type(e).__name__})'
                    )
                    continue
                bits = []
                if name_changed:
                    bits.append(f'renamed from "{cur_name}"')
                if desc_changed:
                    bits.append("description updated")
                if due_changed:
                    # cur_due == today means the event was due *today* right up
                    # until this edit — i.e. a "due today" alert (morning
                    # briefing / deadline_warning) may already have gone out
                    # earlier the same day. Without this, the reconcile line
                    # reads as a routine update instead of a contradiction of
                    # something Simon was just told (task-postg2ap-3hnvkz,
                    # 2026-09-12: told "due today" at 09:00, silently moved to
                    # 09-19 at 20:00, no connection drawn between the two).
                    if cur_due == today.isoformat():
                        bits.append(
                            f"due moved to {want_due} — reverses today's "
                            f"due-today alert, that one's now stale"
                        )
                    else:
                        bits.append(f"due moved to {want_due}")
                if needs_id:
                    bits.append("belki_id backfilled onto legacy event")
                if needs_project:
                    bits.append(f"tagged to {t['project']}")
                if priority_changed:
                    bits.append(f"priority set to {t['priority']}")
                icon = "🔧" if (needs_id or needs_project) else "🔄"
                reconciled_lines.append(f"{icon} {t['name']} — {', '.join(bits)}")

        # Persist freed evenings now, not only in the end-of-sync write below —
        # that write is skipped whenever nothing new gets imported this cycle
        # (see the early `if not new_tasks: return` below), which would otherwise
        # let a just-freed evening silently revert on the next sync's reload.
        if spent_dirty:
            ledger.set_state(_week_key(week), json.dumps({"alloc": alloc, "spent": spent}))

        # Pick this week's work: each project contributes tasks in Belki file
        # order (usually a dependency order) until its evening budget is used up.
        # A project already holding evenings in the target week has that counted
        # against its budget, so a mid-week sync tops up rather than doubling.
        week_key = week.isoformat()
        new_tasks: list[tuple[dict, str]] = []
        for pname in sorted(alloc, key=lambda p: (-alloc[p], p)):
            used = spent.get(pname, 0)
            for t in importable(pname):
                if is_fixed(t):
                    # A hard date from Belki isn't the allocator's to move.
                    new_tasks.append((t, pname))
                    continue
                need = _need(t)
                if used + need > alloc[pname]:
                    break  # stop, don't skip — skipping would reorder the backlog
                used += need
                spent[pname] = used
                new_tasks.append((t, pname))

        if not new_tasks:
            pre_lines = alloc_lines + reconciled_lines + blocked_lines + (
                [vanished_line] if vanished_line else []
            )
            prefix = "\n".join(pre_lines) + "\n\n" if pre_lines else ""
            suffix = (
                "\n\nParser notes (read, but ignored):\n"
                + "\n".join(f"  • {n}" for n in notes[:5])
                if notes
                else ""
            )
            return finish(
                f"{prefix}Nothing new to schedule — this week's evenings are already "
                f"committed." + suffix,
                0,
                len(reconciled_lines),
            )

        usage = week_usage(deadlines)
        slot = week

        for t, _ in new_tasks:
            if is_fixed(t):
                wk = _next_sunday_on_or_after(
                    datetime.date.fromisoformat(t["due"])
                ).isoformat()
                usage[wk] = usage.get(wk, 0) + _need(t)

        lines = []
        if alloc_lines:
            lines.extend(alloc_lines)
            lines.append("")
        if reconciled_lines:
            lines.extend(reconciled_lines)
            lines.append("")
        if blocked_lines:
            lines.extend(blocked_lines)
            lines.append("")
        lines.append(f"📥 Scheduled {len(new_tasks)} task(s) for the week of {week_key}:")

        imported = 0
        for t, pname in new_tasks:
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
            try:
                insert_deadline(
                    f"{ledger.DEADLINE_PREFIX} {t['name']} — DUE",
                    f"{due}T23:59:00",
                    f"{due}T23:59:00",
                    origin="belki_import",
                    estimate=t["estimate"],
                    body_text=t["description"] or None,
                    project=pname,
                    belki_id=t["id"],
                    priority=t["priority"],
                )
            except Exception as e:
                sync_errors.append(
                    f'could not import "{t["name"]}" ({type(e).__name__})'
                )
                continue
            imported += 1
            est_note = (
                f" (est: {t['estimate']} evening{'s' if t['estimate'] != 1 else ''})"
                if t["estimate"] is not None
                else " (no estimate — fills its week)"
            )
            lines.append(f"• [{pname}] {t['name']} — due {due}{est_note}{note}")
            if t["estimate"] is not None and t["estimate"] > EVENINGS_PER_WEEK:
                lines.append(
                    f"  ⚠️ estimated {t['estimate']} evenings exceeds your weekly capacity "
                    f"of {EVENINGS_PER_WEEK} — consider splitting it in Belki."
                )

        # Record the spend against the week. Without this the next sync would
        # re-derive it from the calendar, where a completed task has vanished —
        # and the evening it used would be handed straight to the next task.
        if imported and not dry_run:
            ledger.set_state(_week_key(week), json.dumps({"alloc": alloc, "spent": spent}))

        if vanished_line:
            lines.append(vanished_line)

        if skipped:
            lines.append("Skipped lines I couldn't parse:")
            lines.extend(f"  ✗ {s}" for s in skipped[:5])
        if notes:
            lines.append("Parser notes (read, but ignored):")
            lines.extend(f"  • {n}" for n in notes[:5])

        return finish("\n".join(lines), imported, len(reconciled_lines))
    except Exception as e:
        # The completion pass above already deleted events and filled
        # completed_lines; without this, an error here (a Calendar 5xx on a
        # reconcile/import write, a stale socket, an allocation edge case)
        # propagated out and every caller just log()'d it — so the user saw
        # the nag stop with no word that anything had completed. Return what
        # we have; STATUS_SYNC_ERROR makes the caller speak up regardless.
        return finish(
            f"Completions applied, but the rest of the sync (imports, "
            f"reconciles) errored partway: {type(e).__name__}: {e}. "
            f"Retrying next cycle.",
            0,
            0,
            status=STATUS_SYNC_ERROR,
        )
