from short_names import (
    button_custom_id,
    derive_short,
    find_collisions,
    normalize_query,
    parse_custom_id,
)


def test_derive_from_id_middle_segment():
    assert derive_short("task-endcard1-4h7m2q", None) == "endcard1"


def test_override_wins_and_is_lowercased():
    assert derive_short("task-1hs4mvme-qxfpw0", " Replay ") == "replay"


def test_no_id_no_override_is_none():
    assert derive_short(None, None) is None
    assert derive_short("", "") is None


def test_id_without_three_segments_falls_back_to_whole_id_minus_prefix():
    assert derive_short("task-legacy", None) == "legacy"
    assert derive_short("weird", None) == "weird"


def test_normalize_query_strips_brackets_space_case():
    assert normalize_query("  [EndCard1] ") == "endcard1"
    assert normalize_query("endcard1") == "endcard1"


def test_find_collisions_only_duplicates():
    pairs = [("a", "Task A"), ("b", "Task B"), ("a", "Task A2")]
    assert find_collisions(pairs) == {"a": ["Task A", "Task A2"]}


def test_custom_id_round_trip():
    cid = button_custom_id("push", "abc123def")
    assert cid == "tether:push:abc123def"
    assert parse_custom_id(cid) == ("push", "abc123def")


def test_parse_custom_id_rejects_unknown():
    assert parse_custom_id("tether:delete:abc") is None
    assert parse_custom_id("other:done:abc") is None
    assert parse_custom_id("tether:done:") is None
