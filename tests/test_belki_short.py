import belki_import

CARD = """- [ ] End card thing
  id:: task-endcard1-4h7m2q
  project:: Tiffy's Classroom
  priority:: P4
- [ ] Replay probe
  id:: task-1hs4mvme-qxfpw0
  short:: replay
  project:: ACC Race Engineer
- [ ] Another end card
  id:: task-endcard1-zzzzzz
  project:: Tiffy's Classroom
"""


def _parse(tmp_path):
    p = tmp_path / "2026-10.md"
    p.write_text(CARD, encoding="utf-8")
    return belki_import.parse_data_file(str(p))


def test_short_field_parsed_and_not_flagged_unknown(tmp_path):
    tasks, _skipped, notes = _parse(tmp_path)
    by_name = {t["name"]: t for t in tasks}
    assert by_name["Replay probe"]["short"] == "replay"
    assert by_name["End card thing"]["short"] is None
    assert not any("short" in n and "unknown" in n.lower() for n in notes)


def test_short_collision_notes(tmp_path):
    tasks, _s, _n = _parse(tmp_path)
    notes = belki_import.short_name_collision_notes(tasks)
    assert len(notes) == 1
    assert '"endcard1"' in notes[0]
    assert "End card thing" in notes[0] and "Another end card" in notes[0]


def test_collision_ignores_done_tasks(tmp_path):
    tasks, _s, _n = _parse(tmp_path)
    tasks[2]["done"] = True
    assert belki_import.short_name_collision_notes(tasks) == []
