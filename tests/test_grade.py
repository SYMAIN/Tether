import datetime
from ledger import replay_grade, grade_letter, GRADE_BASELINE

D = datetime.date(2026, 11, 1)


def row(day, action, name="t", due=None):
    return (f"{day}T12:00:00", action, name, due)


def test_empty_is_baseline():
    assert replay_grade([], D) == GRADE_BASELINE


def test_repeat_pushes_cost_more():
    one = replay_grade([row("2026-11-01", "pushed")], D)
    two = replay_grade([row("2026-11-01", "pushed")] * 2, D)
    assert GRADE_BASELINE - one < (one - two)


def test_nag_day_cost_is_capped():
    many = [row("2026-11-01", "nag_ignored", f"t{i}") for i in range(10)]
    assert replay_grade(many, D) == GRADE_BASELINE - 6


def test_completion_on_time_beats_late():
    on = replay_grade([row("2026-11-01", "completed", due="2026-11-02")], D)
    late = replay_grade([row("2026-11-01", "completed", due="2026-10-30")], D)
    assert on > late > GRADE_BASELINE


def test_drifts_back_toward_baseline():
    rows = [row("2026-10-01", "pushed")] * 3
    low = replay_grade(rows, datetime.date(2026, 10, 1))
    later = replay_grade(rows, D)
    assert low < later < GRADE_BASELINE


def test_floor_and_letters():
    rows = [row("2026-11-01", "pushed")] * 50
    assert replay_grade(rows, D) == 0
    assert grade_letter(95) == "A" and grade_letter(80) == "B-" and grade_letter(10) == "F"
