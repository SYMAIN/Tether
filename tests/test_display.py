"""display.py is the whole of the bot's wording layer, so it gets its own tests.

Every example in the design brief is pinned here, plus the edges that made the
helpers worth writing (a separator too early to cut at, a title exactly at
max_len, a project prefix that isn't actually there).
"""

import datetime

from display import fmt_date, fmt_time, fmt_times, short_title

TODAY = datetime.date(2026, 11, 8)  # a Sunday


# --- short_title ---
def test_drops_project_prefix_then_detail_clause():
    assert (
        short_title(
            "ACC Race Engineer: Replay probe - does ACC shared memory update "
            "during replay playback?",
            "ACC Race Engineer",
        )
        == "Replay probe"
    )


def test_cuts_at_colon():
    assert (
        short_title(
            "Fix ability-spam: track real hotkey-slot order instead of blindly "
            "pressing every key"
        )
        == "Fix ability-spam"
    )


def test_cuts_at_colon_with_slash_in_stem():
    assert short_title("Sulfur springs/hard: 19 of 34 runs died...") == "Sulfur springs/hard"


def test_cuts_at_last_space_when_too_long():
    assert (
        short_title(
            "Replace Skills-tree Ionicons glyphs with exercise-representative "
            "art — DEFERRED post-launch"
        )
        == "Replace Skills-tree Ionicons glyphs with…"
    )


def test_no_separator_is_left_alone():
    assert short_title("Email the landlord") == "Email the landlord"


def test_separator_too_early_is_not_a_cut_point():
    # ": " at index 6 would leave "Fix it" — the floor keeps the whole title.
    assert short_title("Fix it: something short") == "Fix it: something short"


def test_exact_max_len_is_not_truncated():
    title = "a" * 40
    assert short_title(title) == title
    assert short_title(title + "b") == "a" * 40 + "…"


def test_long_word_without_spaces_is_hard_cut():
    assert short_title("x" * 60) == "x" * 40 + "…"


def test_absent_project_prefix_is_left_alone():
    assert short_title("Replay probe", "Some Other Project") == "Replay probe"


def test_project_prefix_match_is_case_insensitive():
    assert short_title("tether: Fix the nag loop", "Tether") == "Fix the nag loop"


def test_trailing_punctuation_is_stripped():
    assert short_title("Read the docs.") == "Read the docs"


def test_empty_name_is_empty():
    assert short_title("") == ""
    assert short_title(None) == ""


# --- fmt_date ---
def test_fmt_date_relative_days():
    assert fmt_date(TODAY, TODAY) == "tonight"
    assert fmt_date(TODAY + datetime.timedelta(days=1), TODAY) == "tomorrow"
    assert fmt_date(TODAY - datetime.timedelta(days=1), TODAY) == "yesterday"


def test_fmt_date_weekday_inside_the_week():
    assert fmt_date(TODAY + datetime.timedelta(days=2), TODAY) == "Tue"
    assert fmt_date(TODAY + datetime.timedelta(days=5), TODAY) == "Fri"
    assert fmt_date(TODAY + datetime.timedelta(days=6), TODAY) == "Sat"


def test_fmt_date_far_out_uses_month_day():
    assert fmt_date(datetime.date(2026, 11, 15), TODAY) == "Nov 15"
    assert fmt_date(datetime.date(2026, 11, 30), TODAY) == "Nov 30"
    assert fmt_date(datetime.date(2026, 12, 1), TODAY) == "Dec 1"


def test_fmt_date_past_days_use_month_day():
    assert fmt_date(datetime.date(2026, 11, 3), TODAY) == "Nov 3"


def test_fmt_date_other_year_is_spelled_out():
    assert fmt_date(datetime.date(2027, 12, 1), TODAY) == "Dec 1, 2027"
    assert fmt_date(datetime.date(2027, 1, 25), TODAY) == "Jan 25, 2027"


# --- fmt_time ---
def test_fmt_time_examples():
    assert fmt_time("14:30") == "2:30pm"
    assert fmt_time("09:00") == "9am"
    assert fmt_time("12:00") == "12pm"
    assert fmt_time("00:00") == "12am"


def test_fmt_time_more_edges():
    assert fmt_time("09:05") == "9:05am"
    assert fmt_time("12:30") == "12:30pm"
    assert fmt_time("18:00") == "6pm"
    assert fmt_time("23:59") == "11:59pm"


def test_fmt_time_unparseable_passes_through():
    assert fmt_time("") == ""
    assert fmt_time(None) == "None"


# --- fmt_times ---
def test_fmt_times_examples():
    assert fmt_times(1) == "once"
    assert fmt_times(2) == "twice"
    assert fmt_times(3) == "3×"
    assert fmt_times(0) == "0×"
