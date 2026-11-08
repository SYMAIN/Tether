"""
display.py — pure text helpers for the DMs Tether sends.

Presentation only: no calendar, no ledger, no Discord, and deliberately no
imports from main.py (main imports this module). The point is that a task
renders the same way in a nag embed, a sync reply, the morning briefing and
the weekly retrospective, and that the formatting is unit-testable without a
bot, a calendar or an API key.

The target is "a person wrote this", not "a system logged this": a 40-char
short title instead of a full Belki card title, "tonight" instead of
"2026-11-08T23:59:00", "9am" instead of "09:00", "twice" instead of "2×".
"""

import datetime

# Where a title stops being a title. The separators are the ones Belki cards
# actually use to hang a detail clause off the front: "Fix ability-spam: <the
# story>", "Replay probe - <the story>", "Thing (v2)".
SEPARATORS = (": ", " — ", " - ", " (")
# A separator this early is part of the sentence, not a cut point — cutting
# "Fix it: ..." at index 6 would leave a 6-char title nobody can recognise.
MIN_STEM = 12
# Trailing punctuation left over from a cut ("...runs died." → "…runs died").
# "…" is deliberately absent: it's the marker we add ourselves.
TRIM_CHARS = " \t\r\n.,;:!?–—"

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
# Spelled out rather than strftime("%a")/("%b"): those follow the host's
# locale, so the same task would render differently on a French-locale
# machine — and the tests would pass or fail depending on where they ran.
_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def _trim(text: str) -> str:
    return text.rstrip(TRIM_CHARS)


def _cut_at_separator(text: str) -> str:
    """Cuts at the earliest separator that leaves a usable stem.

    Earliest-with-a-long-enough-stem, not plain earliest: "Fix it: the real
    story - and more" has a ": " at index 6 that's part of the sentence; the
    first separator worth cutting at is the " - " much further along.
    """
    best = None
    for sep in SEPARATORS:
        idx = text.find(sep)
        if idx >= MIN_STEM and (best is None or idx < best):
            best = idx
    return text[:best] if best is not None else text


def short_title(name: str, project: str | None = None, max_len: int = 40) -> str:
    """A title short enough to read at a glance in a DM.

    ("ACC Race Engineer: Replay probe - does ACC shared memory update during
    replay playback?", "ACC Race Engineer") -> "Replay probe"
    ("Fix ability-spam: track real hotkey-slot order instead of blindly
    pressing every key") -> "Fix ability-spam"
    ("Sulfur springs/hard: 19 of 34 runs died...") -> "Sulfur springs/hard"
    ("Replace Skills-tree Ionicons glyphs with exercise-representative art —
    DEFERRED post-launch") -> "Replace Skills-tree Ionicons glyphs with…"

    The full title is never lost: it stays on the calendar event, and the
    handle in parentheses is what Simon types back.
    """
    text = (name or "").strip()
    if project:
        prefix = f"{project}: "
        if text.lower().startswith(prefix.lower()):
            text = text[len(prefix):].strip()
    text = _cut_at_separator(text)
    if len(text) > max_len:
        # The last space in the first max_len+1 chars: a title whose 40th
        # character is the space itself keeps the word that just fit, rather
        # than dropping to the previous one.
        cut = text.rfind(" ", 0, max_len + 1)
        text = _trim(text[: cut if cut > 0 else max_len]).rstrip("…")
        return _trim(text) + "…"
    return _trim(text)


def fmt_date(d: datetime.date, today: datetime.date) -> str:
    """A date as it would be said: tonight / tomorrow / yesterday / Fri / Nov 15.

    Spelled out as "tonight", "tomorrow", "yesterday", a weekday inside the
    next week, else "Nov 15" (or "Jan 25, 2027" once the year differs).

    Weekday names only inside the next week — "Fri" is a real answer to "when
    is that due" for something a few days out, and useless for something three
    weeks away. A different year is spelled out, because "(Dec 1)" next to a
    December task list is a trap when the year has rolled over.
    """
    delta = (d - today).days
    if delta == 0:
        return "tonight"
    if delta == 1:
        return "tomorrow"
    if delta == -1:
        return "yesterday"
    if 2 <= delta <= 6:
        return _WEEKDAYS[d.weekday()]
    label = f"{_MONTHS[d.month - 1]} {d.day}"
    return f"{label}, {d.year}" if d.year != today.year else label


def fmt_time(hhmm: str) -> str:
    """A clock time as it would be said: 14:30 -> 2:30pm, 09:00 -> 9am.

    "12:00" -> "12pm" and "00:00" -> "12am" — both midnight/noon cases are
    where a naive hour-12 conversion says "0pm".
    Anything that isn't HH:MM is passed through untouched — this runs on
    calendar data and a raise here would cost the whole DM it's part of.
    """
    try:
        hour, minute = (int(p) for p in str(hhmm).split(":")[:2])
    except (TypeError, ValueError):
        return str(hhmm)
    suffix = "am" if hour % 24 < 12 else "pm"
    hour12 = hour % 12 or 12
    return f"{hour12}:{minute:02d}{suffix}" if minute else f"{hour12}{suffix}"


def fmt_times(times: int) -> str:
    """1 -> "once", 2 -> "twice", n -> "3×"."""
    if times == 1:
        return "once"
    if times == 2:
        return "twice"
    return f"{times}×"
