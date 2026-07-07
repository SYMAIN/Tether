"""
ledger.py — persistent task lifecycle history for Tether.

Every calendar mutation in main.py records a row here through deterministic
Python hooks (never LLM-initiated). The read side powers the weekly
retrospective. A broken ledger must never break the bot: all writes swallow
exceptions and log to tether.log instead of raising.

Run `python ledger.py` to print the retrospective as a smoke test.
"""

import datetime
import json
import os
import re
import sqlite3
import time

import pytz

TORONTO_TZ = pytz.timezone("America/Toronto")
DEADLINE_PREFIX = "⏰"
DB_PATH = os.environ.get("LEDGER_DB", "data/ledger.db")
LOG_FILE = "tether.log"

# --- SHARED TASK HELPERS (single home; main.py and test_jobs.py import these) ---
COMPLEXITY_HIGH = [
    "exam",
    "final",
    "midterm",
    "thesis",
    "dissertation",
    "project",
    "build",
    "app",
    "bot",
    "feature",
    "system",
    "redesign",
    "research",
]
COMPLEXITY_MED = [
    "report",
    "essay",
    "assignment",
    "lab",
    "presentation",
    "study",
    "review",
    "analysis",
    "proposal",
]
COMPLEXITY_LOW = [
    "email",
    "reply",
    "read",
    "watch",
    "call",
    "meeting",
    "form",
    "submit",
]


def infer_complexity(task_title: str) -> int:
    title = (task_title or "").lower()
    if any(w in title for w in COMPLEXITY_HIGH):
        return 3
    if any(w in title for w in COMPLEXITY_MED):
        return 2
    return 1


def complexity_label(score: int) -> str:
    return {3: "high complexity", 2: "medium complexity", 1: "quick task"}.get(
        score, "unknown"
    )


def clean_name(summary: str) -> str:
    return (summary or "").replace(DEADLINE_PREFIX, "").replace("— DUE", "").strip()


# --- DB ---
_SCHEMA = """
CREATE TABLE IF NOT EXISTS task_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    action      TEXT NOT NULL,
    event_id    TEXT,
    task_name   TEXT,
    complexity  INTEGER,
    due_date    TEXT,
    new_date    TEXT,
    reason      TEXT,
    origin      TEXT,
    extra       TEXT
);
CREATE INDEX IF NOT EXISTS idx_te_name ON task_events(task_name COLLATE NOCASE);
CREATE INDEX IF NOT EXISTS idx_te_action_ts ON task_events(action, ts);
CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def _log(entry: str):
    try:
        timestamp = datetime.datetime.now(TORONTO_TZ).strftime("%Y-%m-%d %H:%M")
        with open(LOG_FILE, "a") as f:
            f.write(f"[{timestamp}] {entry}\n")
    except Exception:
        pass


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH, timeout=5)


def init_db():
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with _connect() as conn:
        conn.executescript(_SCHEMA)


# --- WRITE SIDE ---
# Session-bump reconciliation: the scheduler session has no "move event" tool,
# so it bumps a task by delete + create. We remember recent deletes by task
# name and convert a matching create into a single 'pushed' row.
_recent_deletes: dict[str, tuple[int, str, float]] = {}
_BUMP_WINDOW_SECONDS = 180


def record(
    action: str,
    *,
    event_id=None,
    task_name=None,
    due_date=None,
    new_date=None,
    reason=None,
    origin=None,
    extra: dict | None = None,
    ts_override: str | None = None,
) -> int | None:
    try:
        ts = ts_override or datetime.datetime.now(TORONTO_TZ).isoformat(
            timespec="seconds"
        )
        complexity = infer_complexity(task_name) if task_name else None
        with _connect() as conn:
            cur = conn.execute(
                "INSERT INTO task_events "
                "(ts, action, event_id, task_name, complexity, due_date, new_date, reason, origin, extra) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ts,
                    action,
                    event_id,
                    task_name,
                    complexity,
                    due_date,
                    new_date,
                    reason,
                    origin,
                    json.dumps(extra) if extra else None,
                ),
            )
            return cur.lastrowid
    except Exception as e:
        _log(f"[LEDGER] record({action}) failed: {e}")
        return None


def _event_fields(event: dict) -> tuple[str | None, str, str | None]:
    name = clean_name(event.get("summary", ""))
    due = (event.get("start", {}) or {}).get("dateTime", "")[:10] or None
    return event.get("id"), name, due


def record_created(event: dict, origin="normal", estimate=None, project=None):
    eid, name, due = _event_fields(event)
    hit = _recent_deletes.pop(name.lower(), None) if name else None
    if hit:
        row_id, old_due, t0 = hit
        if time.monotonic() - t0 <= _BUMP_WINDOW_SECONDS and due:
            try:
                with _connect() as conn:
                    conn.execute(
                        "UPDATE task_events "
                        "SET action='pushed', due_date=?, new_date=?, origin='session_bump' "
                        "WHERE id=?",
                        (old_due, due, row_id),
                    )
                return
            except Exception as e:
                _log(f"[LEDGER] session-bump reconcile failed: {e}")
    extra = {}
    if estimate is not None:
        extra["estimate"] = estimate
    if project:
        extra["project"] = project
    record(
        "created",
        event_id=eid,
        task_name=name,
        due_date=due,
        origin=origin,
        extra=extra or None,
    )


def record_deleted(event: dict):
    eid, name, due = _event_fields(event)
    row_id = record("deleted", event_id=eid, task_name=name, due_date=due)
    if row_id and name:
        now = time.monotonic()
        for k in [
            k
            for k, (_, _, t) in _recent_deletes.items()
            if now - t > _BUMP_WINDOW_SECONDS
        ]:
            _recent_deletes.pop(k, None)
        _recent_deletes[name.lower()] = (row_id, due, now)


def record_pushed(event: dict, new_date: str, reason: str, origin: str, old_due=None):
    eid, name, due = _event_fields(event)
    record(
        "pushed",
        event_id=eid,
        task_name=name,
        due_date=old_due or due,
        new_date=new_date,
        reason=reason,
        origin=origin,
    )


def record_completed(event: dict):
    eid, name, due = _event_fields(event)
    record("completed", event_id=eid, task_name=name, due_date=due)


def record_kept(event: dict):
    eid, name, due = _event_fields(event)
    record("kept", event_id=eid, task_name=name, due_date=due)


def record_nag_ignored(event: dict, count: int):
    eid, name, due = _event_fields(event)
    record(
        "nag_ignored",
        event_id=eid,
        task_name=name,
        due_date=due,
        origin="midnight",
        extra={"nag_count": count},
    )


def backfill_from_events(events: list, parse_meta) -> int:
    """One-time seed from existing calendar metadata. No-op unless the table is empty.

    Push dates are unrecoverable from TETHER_META, so only 'created' rows are
    written (origin='backfill', prior_pushes in extra); slip/on-time stats
    exclude backfill rows.
    """
    try:
        with _connect() as conn:
            if conn.execute("SELECT COUNT(*) FROM task_events").fetchone()[0]:
                return 0
    except Exception as e:
        _log(f"[LEDGER] backfill precheck failed: {e}")
        return 0
    inserted = 0
    seen = set()
    for e in events:
        eid, name, due = _event_fields(e)
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        meta = parse_meta(e)
        created_at = meta.get("created_at", "")
        ts = f"{created_at}T00:00:00" if created_at else None
        row = record(
            "created",
            event_id=eid,
            task_name=name,
            due_date=due,
            origin="backfill",
            extra={"prior_pushes": meta.get("pushes", 0)},
            ts_override=ts,
        )
        if row:
            inserted += 1
    return inserted


# --- STATE (single-active-project etc.) ---
def get_state(key: str, default=None):
    try:
        with _connect() as conn:
            row = conn.execute(
                "SELECT value FROM state WHERE key=?", (key,)
            ).fetchone()
            return row[0] if row else default
    except Exception as e:
        _log(f"[LEDGER] get_state({key}) failed: {e}")
        return default


def set_state(key: str, value: str):
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT INTO state (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )
    except Exception as e:
        _log(f"[LEDGER] set_state({key}) failed: {e}")


# --- READ SIDE ---
def _cutoff(days: int) -> str:
    return (
        datetime.datetime.now(TORONTO_TZ) - datetime.timedelta(days=days)
    ).isoformat(timespec="seconds")


def push_count(task_name: str) -> int:
    try:
        with _connect() as conn:
            return conn.execute(
                "SELECT COUNT(*) FROM task_events "
                "WHERE action='pushed' AND task_name=? COLLATE NOCASE",
                (task_name,),
            ).fetchone()[0]
    except Exception:
        return 0


def completed_names() -> set:
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT lower(task_name) FROM task_events "
                "WHERE action='completed' AND task_name IS NOT NULL"
            ).fetchall()
            return {r[0] for r in rows}
    except Exception:
        return set()


def weekly_counts(days: int = 7) -> dict:
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT action, COUNT(*) FROM task_events "
                "WHERE ts >= ? AND origin IS NOT 'backfill' GROUP BY action",
                (_cutoff(days),),
            ).fetchall()
            return dict(rows)
    except Exception:
        return {}


def avg_slip_days(complexity: int | None = None, days: int = 90) -> float | None:
    try:
        q = (
            "SELECT AVG(julianday(new_date) - julianday(due_date)) FROM task_events "
            "WHERE action='pushed' AND origin IS NOT 'backfill' "
            "AND new_date IS NOT NULL AND due_date IS NOT NULL AND ts >= ?"
        )
        params: list = [_cutoff(days)]
        if complexity is not None:
            q += " AND complexity=?"
            params.append(complexity)
        with _connect() as conn:
            val = conn.execute(q, params).fetchone()[0]
            return round(val, 1) if val is not None else None
    except Exception:
        return None


def on_time_rate(days: int = 30) -> tuple[int, int] | None:
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT ts, due_date FROM task_events "
                "WHERE action='completed' AND origin IS NOT 'backfill' "
                "AND due_date IS NOT NULL AND ts >= ?",
                (_cutoff(days),),
            ).fetchall()
    except Exception:
        return None
    if not rows:
        return None
    on_time = sum(1 for ts, due in rows if ts[:10] <= due)
    return on_time, len(rows)


def _is_open(conn: sqlite3.Connection, task_name: str) -> bool:
    row = conn.execute(
        "SELECT MAX(CASE WHEN action='created' THEN id END), "
        "MAX(CASE WHEN action IN ('completed','deleted') THEN id END) "
        "FROM task_events WHERE task_name=? COLLATE NOCASE",
        (task_name,),
    ).fetchone()
    last_created, last_closed = row
    if last_closed is None:
        return True
    return last_created is not None and last_created > last_closed


def most_pushed_open_task(days: int = 90) -> tuple[str, int] | None:
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT task_name, COUNT(*) AS c FROM task_events "
                "WHERE action='pushed' AND ts >= ? AND task_name IS NOT NULL "
                "GROUP BY task_name COLLATE NOCASE ORDER BY c DESC",
                (_cutoff(days),),
            ).fetchall()
            for name, count in rows:
                if _is_open(conn, name):
                    return name, count
    except Exception:
        pass
    return None


def _normalize_reason(reason: str) -> str:
    r = re.sub(r"[^\w\s]", "", (reason or "").lower())
    return re.sub(r"\s+", " ", r).strip()


def recurring_reasons(min_count: int = 3, days: int = 90) -> list[tuple[str, str, int]]:
    """(task_name, reason, count) for tasks pushed min_count+ times citing the same reason."""
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT task_name, reason FROM task_events "
                "WHERE action='pushed' AND ts >= ? "
                "AND task_name IS NOT NULL AND reason IS NOT NULL",
                (_cutoff(days),),
            ).fetchall()
    except Exception:
        return []
    counts: dict[tuple[str, str], list] = {}
    for name, reason in rows:
        norm = _normalize_reason(reason)
        if not norm:
            continue
        key = (name.lower(), norm)
        entry = counts.setdefault(key, [name, reason, 0])
        entry[2] += 1
    return [
        (name, reason, c) for (name, reason, c) in counts.values() if c >= min_count
    ]


def push_clusters(days: int = 30) -> dict[int, int]:
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT complexity, COUNT(*) FROM task_events "
                "WHERE action='pushed' AND ts >= ? AND complexity IS NOT NULL "
                "GROUP BY complexity",
                (_cutoff(days),),
            ).fetchall()
            return dict(rows)
    except Exception:
        return {}


def retro_lines(min_rows: int = 5) -> list[str]:
    """Deterministic stats block for the weekly summary."""
    try:
        with _connect() as conn:
            total = conn.execute(
                "SELECT COUNT(*) FROM task_events WHERE origin IS NOT 'backfill'"
            ).fetchone()[0]
    except Exception as e:
        _log(f"[LEDGER] retro failed: {e}")
        return []
    if total < min_rows:
        return ["📈 History: not enough data yet."]

    lines = []
    wc = weekly_counts(7)
    lines.append(
        f"📈 **Last 7 days:** {wc.get('created', 0)} created · "
        f"{wc.get('completed', 0)} completed · {wc.get('pushed', 0)} pushes"
    )
    rate = on_time_rate(30)
    if rate:
        on, tot = rate
        lines.append(
            f"**On-time (30d):** {round(100 * on / tot)}% ({on}/{tot} done by their due date)"
        )
    slip = avg_slip_days(days=30)
    if slip is not None:
        lines.append(f"**Avg slip per push (30d):** {slip} days")
    top = most_pushed_open_task()
    if top:
        lines.append(f"**Most-pushed open task:** {top[0]} — {top[1]}×")
    clusters = push_clusters(30)
    if clusters:
        lines.append(
            "**Pushes by type (30d):** "
            f"high {clusters.get(3, 0)} · medium {clusters.get(2, 0)} · quick {clusters.get(1, 0)}"
        )
    for name, reason, count in recurring_reasons():
        lines.append(f'⚠️ **{name}** pushed {count}× citing "{reason}" — re-scope or drop.')
    return lines


if __name__ == "__main__":
    init_db()
    print("\n".join(retro_lines()))
