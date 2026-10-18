"""allocate_evenings() must name eligible projects that aren't in projects.md.

Such a project is deliberately still scheduled (advisory registry), but the
weekly allocation message is the only place the missing projects.md line shows
up — silence there is how a typo'd name goes unnoticed for weeks.
"""

import datetime

import belki_import

MARK = "❓"
TODAY = datetime.date(2026, 10, 18)


def task(name: str, project: str) -> dict:
    return {
        "name": name,
        "project": project,
        "done": False,
        "estimate": 1,
        "blocked_by": None,
        "status": None,
        "priority": "P2",
    }


def marked(lines: list) -> list:
    return [line for line in lines if line.startswith(MARK)]


def test_unregistered_project_is_named():
    projects = {"Alpha": {"due": None, "status": "active"}}
    tasks = [task("alpha card", "Alpha"), task("bravo card", "Bravo")]

    _alloc, lines = belki_import.allocate_evenings(projects, tasks, TODAY, capacity=7)

    hits = marked(lines)
    assert len(hits) == 1
    assert "Bravo" in hits[0]
    assert "Alpha" not in hits[0]
    # The note sits directly above the per-project bullets, not below them.
    assert lines.index(hits[0]) < len(lines) - 1


def test_all_registered_projects_produce_no_note():
    projects = {
        "Alpha": {"due": None, "status": "active"},
        "Bravo": {"due": None, "status": "active"},
    }
    tasks = [task("alpha card", "Alpha"), task("bravo card", "Bravo")]

    _alloc, lines = belki_import.allocate_evenings(projects, tasks, TODAY, capacity=7)

    assert marked(lines) == []


def test_empty_registry_produces_no_note():
    projects = {}
    tasks = [task("alpha card", "Alpha"), task("bravo card", "Bravo")]

    _alloc, lines = belki_import.allocate_evenings(projects, tasks, TODAY, capacity=7)

    assert marked(lines) == []


def test_paused_and_registered_projects_are_not_named():
    projects = {
        "Alpha": {"due": None, "status": "active"},
        "Papa": {"due": None, "status": "paused"},
    }
    tasks = [
        task("alpha card", "Alpha"),
        task("papa card", "Papa"),
        task("bravo card", "Bravo"),
    ]

    _alloc, lines = belki_import.allocate_evenings(projects, tasks, TODAY, capacity=7)

    hits = marked(lines)
    assert len(hits) == 1
    assert hits[0].endswith(": Bravo.")
