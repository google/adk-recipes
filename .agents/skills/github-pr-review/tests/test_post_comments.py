"""The posting gate. Every failure here reaches a colleague's PR."""

import post_comments as pc

PATCH = "@@ -1,3 +1,5 @@\n ctx1\n+added2\n+added3\n ctx4\n-removed\n"


def test_commentable_lines_are_added_plus_context():
    right, _ = pc.commentable_lines(PATCH)
    assert right == {1, 2, 3, 4}


def test_deleted_lines_are_left_side_only():
    """A deleted line is addressable, but only with side=LEFT."""
    right, left = pc.commentable_lines(PATCH)
    assert 3 in left  # the removed line, old numbering
    assert 5 not in right


def test_empty_patch_yields_nothing():
    assert pc.commentable_lines("") == (set(), set())


def test_multi_hunk_patch_tracks_both_ranges():
    patch = "@@ -1,1 +1,1 @@\n ctx\n@@ -50,2 +50,2 @@\n ctx50\n+new51\n"
    right, _ = pc.commentable_lines(patch)
    assert {1, 50, 51} <= right
    assert 25 not in right


def test_no_newline_marker_does_not_advance_numbering():
    right, _ = pc.commentable_lines(
        "@@ -1,1 +1,1 @@\n+a\n\\ No newline at end of file\n"
    )
    assert right == {1}


def test_state_roundtrip_enables_resume(tmp_path):
    p = tmp_path / "c.json.posted"
    pc.save_state(str(p), {"posted": ["a.py:1"]})
    assert pc.load_state(str(p))["posted"] == ["a.py:1"]


def test_missing_state_file_starts_empty(tmp_path):
    assert pc.load_state(str(tmp_path / "nope"))["posted"] == []


def test_key_of_is_stable_and_distinguishes_lines():
    a = {"path": "x.py", "line": 1, "body": "b"}
    b = {"path": "x.py", "line": 2, "body": "b"}
    assert pc.key_of(a) == pc.key_of(dict(a))
    assert pc.key_of(a) != pc.key_of(b)
