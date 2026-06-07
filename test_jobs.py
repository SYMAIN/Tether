"""
test_jobs.py — run scheduled jobs on demand without starting the bot.
Usage: python test_jobs.py [morning|eod|inactivity|queue|nag|debug|all]
Defaults to 'all' if no argument given.
"""

import asyncio
import sys
import os
import datetime
import re

import discord
import pytz
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.discovery import build

# --- CONFIG (mirrors main.py) ---
SCOPES = ["https://www.googleapis.com/auth/calendar"]
DEADLINE_PREFIX = "⏰"
TORONTO_TZ = pytz.timezone("America/Toronto")
DISCORD_USER_ID = int(os.environ.get("DISCORD_USER_ID"))
LOG_FILE = "tether.log"

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

# Nag state for test
unacknowledged_overdue: set = set()
nag_count: int = 0


# --- CALENDAR ---
def get_calendar_service():
    creds = None
    if os.path.exists("token.json"):
        creds = Credentials.from_authorized_user_file("token.json", SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
            with open("token.json", "w") as f:
                f.write(creds.to_json())
        else:
            raise Exception("No valid credentials.")
    return build("calendar", "v3", credentials=creds)


service = get_calendar_service()


def parse_meta(event: dict) -> dict:
    desc = event.get("description", "") or ""
    meta = {
        "pushes": 0,
        "created_at": "",
        "last_modified": "",
        "origin": "normal",
        "nag_ignored": 0,
        "last_push_reason": "",
    }
    match = re.search(r"\[TETHER_META\](.*?)(\[|$)", desc, re.DOTALL)
    if not match:
        return meta
    for line in match.group(1).strip().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            meta[k.strip()] = v.strip()
    meta["pushes"] = int(meta.get("pushes", 0))
    meta["nag_ignored"] = int(meta.get("nag_ignored", 0))
    return meta


def list_upcoming_events(max_results: int = 20):
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    result = (
        service.events()
        .list(
            calendarId="primary",
            timeMin=now,
            maxResults=max_results,
            singleEvents=True,
            orderBy="startTime",
            timeZone="America/Toronto",
        )
        .execute()
    )
    return result.get("items", [])


def get_tether_deadlines():
    return [
        e
        for e in list_upcoming_events(50)
        if e.get("summary", "").startswith(DEADLINE_PREFIX)
    ]


def get_events_in_window(hours_from_now: int):
    now = datetime.datetime.now(TORONTO_TZ)
    cutoff = now + datetime.timedelta(hours=hours_from_now)
    deadlines = []
    for e in get_tether_deadlines():
        start_str = e.get("start", {}).get("dateTime")
        if not start_str:
            continue
        start = datetime.datetime.fromisoformat(start_str)
        if start.tzinfo is None:
            start = TORONTO_TZ.localize(start)
        if now <= start <= cutoff:
            deadlines.append(e)
    return deadlines


def get_overdue_tether_events():
    now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    forty_five_days_ago = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=45)
    ).isoformat()
    all_items = []
    page_token = None
    while True:
        params = dict(
            calendarId="primary",
            timeMin=forty_five_days_ago,
            timeMax=now_utc,
            maxResults=250,
            singleEvents=True,
            orderBy="startTime",
        )
        if page_token:
            params["pageToken"] = page_token
        result = service.events().list(**params).execute()
        all_items.extend(result.get("items", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    overdue = []
    for e in all_items:
        if not e.get("summary", "").startswith(DEADLINE_PREFIX):
            continue
        if e.get("end", {}).get("dateTime"):
            overdue.append(e)
    return overdue


def infer_complexity(task_title: str) -> int:
    title = task_title.lower()
    if any(w in title for w in COMPLEXITY_HIGH):
        return 3
    if any(w in title for w in COMPLEXITY_MED):
        return 2
    return 1


def priority_score(event: dict) -> float:
    today = datetime.datetime.now(TORONTO_TZ).date()
    meta = parse_meta(event)
    pushes = meta["pushes"]
    due_str = event.get("start", {}).get("dateTime", "")[:10]
    try:
        due_date = datetime.date.fromisoformat(due_str)
        days_until = max((due_date - today).days, 0)
    except ValueError:
        days_until = 999
    name = (
        event.get("summary", "")
        .replace(DEADLINE_PREFIX, "")
        .replace("— DUE", "")
        .strip()
    )
    complexity = infer_complexity(name)
    return days_until - (complexity * 2) - (pushes * 1.5)


def rank_deadlines(deadlines: list) -> list:
    return sorted(deadlines, key=priority_score)


def priority_reason(event: dict) -> str:
    today = datetime.datetime.now(TORONTO_TZ).date()
    meta = parse_meta(event)
    pushes = meta["pushes"]
    due_str = event.get("start", {}).get("dateTime", "")[:10]
    try:
        due_date = datetime.date.fromisoformat(due_str)
        days_until = (due_date - today).days
    except ValueError:
        days_until = 999
    complexity = infer_complexity(
        event.get("summary", "")
        .replace(DEADLINE_PREFIX, "")
        .replace("— DUE", "")
        .strip()
    )
    labels = {3: "high complexity", 2: "medium complexity", 1: "quick task"}
    parts = []
    if days_until <= 2:
        parts.append("due very soon")
    elif days_until <= 7:
        parts.append(f"due in {days_until} days")
    else:
        parts.append(f"due {due_str}")
    parts.append(labels.get(complexity, "unknown"))
    if pushes > 0:
        parts.append(f"pushed {pushes}×")
    return ", ".join(parts)


# --- NAG WORDING ---
def nag_message(event: dict, session_nag_count: int) -> str:
    meta = parse_meta(event)
    pushes = meta["pushes"]
    nag_ignored = meta.get("nag_ignored", 0)
    name = (
        event.get("summary", "")
        .replace(DEADLINE_PREFIX, "")
        .replace("— DUE", "")
        .strip()
    )
    desc = event.get("description", "") or ""
    orig_match = re.search(r"Originally due: (\d{4}-\d{2}-\d{2})", desc)
    orig_str = (
        f" (originally due {orig_match.group(1)})" if orig_match and pushes > 0 else ""
    )
    last_reason = meta.get("last_push_reason", "")
    reason_str = f' Last reason: "{last_reason}".' if last_reason else ""

    total_pressure = pushes + nag_ignored + session_nag_count

    if total_pressure == 0:
        return f"⚠️ **{name}** wasn't completed{orig_str}. Keep or push back? (`@Tether push {name} because <reason>` — I'll pick the date)"
    elif total_pressure <= 2:
        return f"⚠️ **{name}** is still sitting there{orig_str}.{reason_str} You haven't responded. Keep or push back?"
    elif total_pressure <= 4:
        return f"🔴 **{name}** — this has slipped {pushes} time(s) and you've ignored {nag_ignored + session_nag_count} reminder(s){orig_str}.{reason_str} What's actually blocking you? Keep or push back."
    elif total_pressure <= 6:
        return f"🔴 **{name}** — still here. Still overdue. You've deferred this {pushes} times{orig_str}.{reason_str} Either commit to a date or delete it. Don't ghost me."
    else:
        return f"🚨 **{name}** — {pushes} pushes. {nag_ignored + session_nag_count} ignored reminders{orig_str}.{reason_str} This task has been rotting. You either do it tonight or you tell me why. I'm not stopping."


def nag_summary_header(nag_count: int) -> str:
    if nag_count == 0:
        return "📋 **Overdue tasks — respond to each one.**\n"
    elif nag_count == 1:
        return "📋 **Still waiting on these. 30 minutes and no response.**\n"
    elif nag_count == 2:
        return "📋 **An hour in. These are still unacknowledged.**\n"
    else:
        return f"📋 **Reminder #{nag_count + 1}. You can't outlast me.**\n"


# --- DISCORD ---
intents = discord.Intents.default()
intents.message_content = True
bot = discord.Client(intents=intents)


async def dm_user(message: str):
    user = await bot.fetch_user(DISCORD_USER_ID)
    await user.send(message)
    print(f"[DM SENT] {message[:200]}")


# --- JOBS ---
async def morning_briefing():
    today = datetime.datetime.now(TORONTO_TZ).date()
    overdue = get_overdue_tether_events()
    overdue_lines = [
        f"• ⚠️ {e['summary'].replace(DEADLINE_PREFIX,'').replace('— DUE','').strip()} — OVERDUE"
        for e in overdue
    ]
    all_events = list_upcoming_events(20)
    today_events = [
        e
        for e in all_events
        if e.get("start", {}).get("dateTime", "").startswith(str(today))
    ]
    cal_lines = [
        f"• {e['start']['dateTime'][11:16]}–{e['end']['dateTime'][11:16]} {e['summary']}"
        for e in today_events
    ]
    deadlines = get_tether_deadlines()
    next_line = ""
    if deadlines:
        top = rank_deadlines(deadlines)[0]
        name = top["summary"].replace(DEADLINE_PREFIX, "").replace("— DUE", "").strip()
        next_line = f"**Next up:** {name} — {priority_reason(top)}."

    lines = ["📅 **Morning briefing**\n"]
    if overdue_lines:
        lines.append("**Overdue:**")
        lines.extend(overdue_lines)
        lines.append("")
    lines.append("**Today's schedule:**")
    lines.extend(cal_lines or ["Nothing on the calendar."])
    lines.append("")
    if next_line:
        lines.append(next_line)
    await dm_user("\n".join(lines))


async def eod_sweep():
    overdue = get_overdue_tether_events()
    if not overdue:
        await dm_user("✅ EOD sweep — no overdue tasks.")
        return
    log_lines = [f"  - {e.get('summary')}" for e in overdue]
    print(f"[EOD] {len(overdue)} overdue tasks:\n" + "\n".join(log_lines))
    await dm_user(
        f"📋 EOD — {len(overdue)} overdue task(s). Nag loop is handling them."
    )


async def inactivity_check():
    deadlines = get_tether_deadlines()
    if not deadlines:
        await dm_user("📋 Inactivity check — queue is empty.")
        return
    today = datetime.datetime.now(TORONTO_TZ).date()
    all_stale = all(
        min(
            (
                today
                - datetime.date.fromisoformat(
                    parse_meta(e).get("last_modified", str(today))
                )
            ).days,
            (
                today
                - datetime.date.fromisoformat(
                    parse_meta(e).get("created_at", str(today))
                )
            ).days,
        )
        >= 5
        for e in deadlines
    )
    if all_stale:
        await dm_user(
            "📋 No queue changes in 5 days. Still accurate? Reply in #tether."
        )
    else:
        await dm_user(
            "📋 Inactivity check — queue has recent activity, no alert needed."
        )


async def weekly_queue_summary():
    deadlines = get_tether_deadlines()
    if not deadlines:
        await dm_user("📋 **Weekly Queue** — No deadlines in the queue. You're clear.")
        return
    ranked = rank_deadlines(deadlines)
    lines = ["📋 **Weekly Queue** _(by priority)_:\n"]
    for i, e in enumerate(ranked):
        name = e["summary"].replace(DEADLINE_PREFIX, "").replace("— DUE", "").strip()
        date_str = e["start"]["dateTime"][:10]
        meta = parse_meta(e)
        pushes = meta["pushes"]
        desc = e.get("description", "") or ""
        orig_match = re.search(r"Originally due: (\d{4}-\d{2}-\d{2})", desc)
        suffix = ""
        if pushes > 0:
            suffix = f" _(pushed {pushes}×"
            if orig_match:
                suffix += f", originally {orig_match.group(1)}"
            suffix += ")_"
        prefix = "🔴" if i == 0 else "•"
        lines.append(f"{prefix} {name} — due {date_str}{suffix}")
    await dm_user("\n".join(lines))


async def test_nag():
    """Simulates 3 nag cycles to verify escalation wording."""
    global nag_count, unacknowledged_overdue
    nag_count = 0
    unacknowledged_overdue = set()

    overdue = get_overdue_tether_events()
    if not overdue:
        print("[NAG TEST] No overdue tasks found — nothing to nag about.")
        await dm_user("📋 Nag test — no overdue tasks found.")
        return

    for e in overdue:
        unacknowledged_overdue.add(e["id"])

    event_map = {e["id"]: e for e in overdue}

    for cycle in range(3):
        print(f"\n[NAG TEST] Cycle {cycle + 1}")
        lines = [nag_summary_header(nag_count)]
        for eid in unacknowledged_overdue:
            if eid in event_map:
                lines.append(nag_message(event_map[eid], nag_count))
        msg = "\n".join(lines)
        await dm_user(msg)
        nag_count += 1


async def debug_overdue():
    now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    forty_five_days_ago = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=45)
    ).isoformat()
    all_items = []
    page_token = None
    while True:
        params = dict(
            calendarId="primary",
            timeMin=forty_five_days_ago,
            timeMax=now_utc,
            maxResults=250,
            singleEvents=True,
            orderBy="startTime",
        )
        if page_token:
            params["pageToken"] = page_token
        result = service.events().list(**params).execute()
        all_items.extend(result.get("items", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    print(f"Total past events in window: {len(all_items)}")
    tether_items = [e for e in all_items if e.get("summary", "").startswith("⏰")]
    print(f"Tether events found: {len(tether_items)}")
    for e in tether_items:
        print(f"  {e.get('summary')} | start: {e.get('start')} | end: {e.get('end')}")


# --- RUNNER ---
JOBS = {
    "morning": morning_briefing,
    "eod": eod_sweep,
    "inactivity": inactivity_check,
    "queue": weekly_queue_summary,
    "nag": test_nag,
    "debug": debug_overdue,
}


@bot.event
async def on_ready():
    print(f"Connected as {bot.user}")
    arg = sys.argv[1] if len(sys.argv) > 1 else "all"
    if arg == "all":
        targets = [
            morning_briefing,
            eod_sweep,
            inactivity_check,
            weekly_queue_summary,
            test_nag,
        ]
    else:
        targets = [JOBS[arg]]
    for job in targets:
        print(f"Running {job.__name__}...")
        await job()
    print("Done.")
    await bot.close()


bot.run(os.environ.get("DISCORD_BOT_TOKEN"))
