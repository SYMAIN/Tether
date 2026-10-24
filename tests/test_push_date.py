import datetime

import os

import pytest

os.environ.setdefault("DISCORD_USER_ID", "1")

main = pytest.importorskip("main")


def _today():
    return datetime.datetime.now(main.TORONTO_TZ).date()


def test_typed_text_date_bypasses_estimate(monkeypatch):
    seen = {}

    def fake_pick(name, reason, queue, original_message=None):
        seen["msg"] = original_message
        return "2099-01-02", False

    monkeypatch.setattr(main, "pick_push_date", fake_pick)
    command = {"target_date": None, "explicit_when": True}
    date, _ = main.resolve_push_date(
        command, {"id": "x"}, {"estimate": "1"}, "t", "r", [], "push t to Friday because r"
    )
    assert date == "2099-01-02"
    assert "Friday" in seen["msg"]


def test_iso_target_date_wins_over_estimate():
    target = (_today() + datetime.timedelta(days=5)).isoformat()
    date, _ = main.resolve_push_date(
        {"target_date": target, "explicit_when": True}, {"id": "x"}, {"estimate": "1"},
        "t", "r", [], "",
    )
    assert date == target
