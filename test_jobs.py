"""
test_jobs.py — run scheduled jobs on demand without starting the full bot.
Usage: python test_jobs.py [morning|eod|inactivity|queue|nag|retro|sync|debug|all]
Defaults to 'all' (the DM-producing jobs) if no argument given.

Thin runner over main.py: it imports the real job functions instead of
keeping parallel copies (main's entrypoint is __main__-guarded), replaces
main's on_ready so the production scheduler, startup catch-up, and startup
sync never run, and connects just long enough to fire the chosen job(s).

'sync' and 'debug' never touch Discord: 'sync' is a dry-run Belki import
preview (no calendar writes, no active-project change), 'debug' dumps the
45-day event window.
"""

import datetime
import os
import sys

import belki_import
import ledger
import main
from main import bot, dm_user


# --- OFFLINE JOBS (no Discord connection) ---
def test_sync():
    """Dry-run Belki sync preview: no calendar writes, no state change."""
    planned = []

    def preview_insert(summary, start, end, origin=None, estimate=None,
                       body_text=None, project=None):
        planned.append(summary)
        return {"id": f"dry{len(planned)}", "summary": summary,
                "start": {"dateTime": start}}

    text, imported, completed = belki_import.sync(
        main.get_tether_deadlines(), preview_insert, dry_run=True
    )
    print(f"[SYNC DRY RUN] would import {imported} task(s), complete {completed}")
    print(text)


def debug_overdue():
    """Dumps the raw 45-day pagination window and the ⏰ events in it."""
    now_utc = datetime.datetime.now(datetime.timezone.utc).isoformat()
    window_start = (
        datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=45)
    ).isoformat()
    all_items = []
    page_token = None
    while True:
        params = dict(
            calendarId="primary",
            timeMin=window_start,
            timeMax=now_utc,
            maxResults=250,
            singleEvents=True,
            orderBy="startTime",
        )
        if page_token:
            params["pageToken"] = page_token
        result = main.get_service().events().list(**params).execute()
        all_items.extend(result.get("items", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    print(f"Total past events in window: {len(all_items)}")
    tether_items = [e for e in all_items if e.get("summary", "").startswith("⏰")]
    print(f"Tether events found: {len(tether_items)}")
    for e in tether_items:
        print(f"  {e.get('summary')} | start: {e.get('start')} | end: {e.get('end')}")


# --- DISCORD JOBS ---
async def test_retro():
    """DMs just the ledger retrospective block."""
    lines = ledger.retro_lines() or ["📈 History: ledger unavailable."]
    await dm_user("\n".join(lines))


async def test_nag():
    """Runs 3 real nag cycles to verify escalation wording."""
    main.nag_count = 0
    main.unacknowledged_overdue.clear()
    main.task_nag_counts.clear()
    if not main.get_overdue_tether_events():
        print("[NAG TEST] No overdue tasks found — nothing to nag about.")
        await dm_user("📋 Nag test — no overdue tasks found.")
        return
    for cycle in range(3):
        print(f"[NAG TEST] Cycle {cycle + 1}")
        await main.send_overdue_nag()


OFFLINE_JOBS = {
    "sync": test_sync,
    "debug": debug_overdue,
}

DISCORD_JOBS = {
    "morning": main.morning_briefing,
    "eod": main.eod_sweep,
    "inactivity": main.inactivity_check,
    "queue": main.weekly_queue_summary,
    "nag": test_nag,
    "retro": test_retro,
}

arg = sys.argv[1] if len(sys.argv) > 1 else "all"
if arg not in OFFLINE_JOBS and arg not in DISCORD_JOBS and arg != "all":
    print(__doc__)
    sys.exit(1)

if arg in OFFLINE_JOBS:
    OFFLINE_JOBS[arg]()
    sys.exit(0)


# Replace main's event handlers for the test connection: no scheduler, no
# startup catch-up/sync, and no reacting to mentions while the real
# container bot is also connected (both sessions receive every event).
@bot.event
async def on_message(message):
    return


@bot.event
async def on_ready():
    print(f"Connected as {bot.user}")
    if arg == "all":
        targets = [
            main.morning_briefing,
            main.eod_sweep,
            main.inactivity_check,
            main.weekly_queue_summary,
            test_nag,
        ]
    else:
        targets = [DISCORD_JOBS[arg]]
    for job in targets:
        print(f"Running {job.__name__}...")
        await job()
    print("Done.")
    await bot.close()


bot.run(os.environ.get("DISCORD_BOT_TOKEN"))
