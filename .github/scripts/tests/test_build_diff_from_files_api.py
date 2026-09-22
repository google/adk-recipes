# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Unit tests for build_diff_from_files_api.py.

This script hands the reviewer a diff that GitHub never sent as a diff. Its
failure modes are quiet in the way that matters most here:

  * a header this script writes differently from the way git writes it parses
    into a path that names no file, so every finding in that file is dropped
    at validation time and the review looks merely thin;
  * an off-by-one in a rebuilt section moves anchors onto real but WRONG
    lines, which still passes validation and gets posted;
  * a dropped record removes a file from the reviewer's view entirely, and a
    clean review of a file nobody read is indistinguishable from a clean file.

So the tests below pin the header shape against the readers that consume it
— post_review_comments.added_line_anchors, which is the real anchor walk
(a thin wrapper over walk_right_side), imported and run rather than imitated
— and pin every path that loses a file to saying so.
"""

import json
import subprocess
import sys
from pathlib import Path

import build_diff_from_files_api as m
import post_review_comments
import pytest

SCRIPT = Path(m.__file__)


def _record(**overrides) -> dict:
    record = {
        "filename": "src/app.py",
        "status": "modified",
        "patch": "@@ -1,2 +1,3 @@\n context\n+added\n unchanged",
        "changes": 1,
    }
    record.update(overrides)
    return record


# ----------------------------------------------------------- header shape


def test_a_modified_file_gets_all_three_header_lines():
    """`diff --git` splits sections, `+++` supplies every anchor's path."""
    section = m.section_for(_record())
    assert section.startswith("diff --git a/src/app.py b/src/app.py\n")
    assert "\n--- a/src/app.py\n" in section
    assert "\n+++ b/src/app.py\n" in section


def test_an_added_file_reads_as_added():
    section = m.section_for(_record(status="added"))
    assert "--- /dev/null" in section
    assert "+++ b/src/app.py" in section


def test_a_deleted_file_reads_as_deleted():
    """`+++ /dev/null` is how the readers recognise a deletion.

    Both of them special-case it — prepare_review_diff drops the section and
    post_review_comments refuses to anchor into it. Emitting `+++ b/path` for
    a deleted file would invite comments on lines that no longer exist.
    """
    section = m.section_for(_record(status="removed"))
    assert "+++ /dev/null" in section
    assert "--- a/src/app.py" in section


def test_a_rename_keeps_both_sides_of_the_name():
    section = m.section_for(
        _record(status="renamed", previous_filename="src/old.py")
    )
    assert section.startswith("diff --git a/src/old.py b/src/app.py\n")
    assert "--- a/src/old.py" in section
    assert "+++ b/src/app.py" in section


def test_no_index_line_is_invented():
    """Nothing downstream reads it, and the SHAs would be fabricated."""
    assert "index " not in m.section_for(_record())


def test_a_record_with_no_patch_yields_no_section():
    assert m.section_for(_record(patch=None)) is None
    assert m.section_for(_record(patch="")) is None


def test_a_patch_without_a_trailing_newline_still_terminates():
    """Two headers sharing a line collapse two files into one section."""
    diff, _ = m.build([_record(), _record(filename="src/other.py")])
    assert "\ndiff --git a/src/other.py" in diff
    assert diff.endswith("\n")
    assert diff.count("diff --git ") == 2


def test_a_patch_with_a_trailing_newline_gains_no_blank_line():
    diff, _ = m.build([_record(patch="@@ -1 +1 @@\n-a\n+b\n")])
    assert "\n\n" not in diff


# ------------------------------------------------------------ path quoting


@pytest.mark.parametrize(
    "path",
    [
        "src/app.py",
        "a/b/c.py",
        "with-dash_and.dot/x.py",
    ],
)
def test_an_ordinary_path_is_left_bare(path):
    """Git quotes only when it must, and so must this."""
    assert m.quote_path(path) == path


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ('weird".py', '"weird\\".py"'),
        ("back\\slash.py", '"back\\\\slash.py"'),
        ("tab\there.py", '"tab\\there.py"'),
        # Non-ASCII goes out as octal UTF-8 bytes, the way git writes it.
        ("caf\u00e9.py", '"caf\\303\\251.py"'),
    ],
)
def test_a_path_needing_quotes_is_quoted_the_way_git_quotes_it(path, expected):
    assert m.quote_path(path) == expected


def test_a_quoted_path_round_trips_through_the_reader():
    """The whole point of matching git: the readers must get the path back.

    prepare_review_diff._unquote_git_path implements git's unquoting. A path
    quoted any other way survives this script and then names no file.
    """
    import prepare_review_diff

    for path in ("caf\u00e9.py", 'weird".py', "back\\slash.py", "tab\there.py"):
        quoted = m.quote_path(path)
        assert prepare_review_diff._unquote_git_path(quoted) == path


# ------------------------------------------------ anchors, the real reader


def test_anchors_land_on_the_lines_the_patch_adds():
    """Run the real consumer, do not imitate it.

    A test that reimplements the anchor walk would agree with a rebuilt diff
    that the actual reviewer pipeline reads differently.
    """
    record = _record(
        patch="@@ -10,3 +10,5 @@\n ctx\n+one\n+two\n ctx2\n ctx3"
    )
    diff, _ = m.build([record])
    anchors = post_review_comments.added_line_anchors(diff)
    # Hunk starts at new-file line 10: ctx=10, one=11, two=12.
    assert anchors == {"src/app.py": {11, 12}}


def test_a_deleted_file_contributes_no_anchors():
    diff, _ = m.build([_record(status="removed", patch="@@ -1,2 +0,0 @@\n-a\n-b")])
    assert post_review_comments.added_line_anchors(diff) == {}


def test_two_files_keep_their_anchors_apart():
    """A section that does not terminate merges the next file's lines in."""
    diff, _ = m.build(
        [
            _record(filename="one.py", patch="@@ -1,1 +1,2 @@\n ctx\n+alpha"),
            _record(filename="two.py", patch="@@ -5,1 +5,2 @@\n ctx\n+beta"),
        ]
    )
    anchors = post_review_comments.added_line_anchors(diff)
    assert anchors == {"one.py": {2}, "two.py": {6}}


def test_an_added_line_that_looks_like_a_header_does_not_break_the_walk():
    """`+++ x` as CONTENT is indistinguishable from a file header by prefix.

    walk_right_side tracks hunk lengths for exactly this reason. The rebuilt
    diff has to state those lengths correctly or the defence stops working.
    """
    diff, _ = m.build(
        [_record(patch="@@ -1,1 +1,3 @@\n ctx\n+++ not a header\n+after")]
    )
    anchors = post_review_comments.added_line_anchors(diff)
    assert anchors == {"src/app.py": {2, 3}}


# ------------------------------------------------------ omissions are loud


def test_files_without_a_patch_are_reported_not_dropped_silently():
    diff, omitted = m.build(
        [
            _record(filename="kept.py"),
            _record(filename="image.webp", patch=None),
            _record(filename="uv.lock", patch=None),
        ]
    )
    assert "kept.py" in diff
    assert omitted == ["image.webp", "uv.lock"]


def test_an_omitted_record_with_no_filename_still_occupies_a_slot():
    """A blank entry in the omission list is better than a missing count."""
    _, omitted = m.build([{"status": "modified"}])
    assert omitted == ["<unknown>"]


def test_a_record_with_a_patch_but_no_filename_is_omitted_not_emitted():
    """A section whose `+++` has no path anchors nothing and confuses both
    readers; count it as omitted so the log says a file was lost."""
    diff, omitted = m.build([_record(filename=None)])
    assert diff == ""
    assert omitted == ["<unknown>"]


# ------------------------------------------------------------ record input


def test_blank_lines_in_the_stream_are_skipped():
    assert m.read_records(["", "  ", json.dumps(_record())]) == [_record()]


def test_malformed_json_is_a_fault_not_a_skipped_file():
    """Skipping it would hand the reviewer a short diff without saying so."""
    with pytest.raises(ValueError, match="line 2 is not JSON"):
        m.read_records([json.dumps(_record()), "{not json"])


def test_a_non_object_record_is_a_fault():
    with pytest.raises(ValueError, match="expected an object"):
        m.read_records(["[1, 2, 3]"])


# ----------------------------------------------------------------- the CLI


def _run(tmp_path: Path, records: list[dict], *extra: str):
    src = tmp_path / "files.jsonl"
    src.write_text(
        "".join(f"{json.dumps(r)}\n" for r in records), encoding="utf-8"
    )
    out = tmp_path / "pr_diff.txt"
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--files",
            str(src),
            "--out",
            str(out),
            *extra,
        ],
        capture_output=True,
        text=True,
    )
    return proc, out


def test_the_cli_writes_a_diff_and_reports_its_counts(tmp_path):
    proc, out = _run(
        tmp_path, [_record(), _record(filename="bin.webp", patch=None)]
    )
    assert proc.returncode == 0, proc.stderr
    assert "diff --git a/src/app.py" in out.read_text(encoding="utf-8")
    assert "files_total=2" in proc.stdout
    assert "files_patched=1" in proc.stdout
    assert "files_omitted=1" in proc.stdout


def test_the_cli_names_the_omitted_files_in_the_log(tmp_path):
    """"18 files omitted" does not say whether that was lockfiles or code."""
    proc, _ = _run(tmp_path, [_record(filename="uv.lock", patch=None)])
    assert "uv.lock" in proc.stdout


def test_the_cli_writes_the_omitted_list_when_asked(tmp_path):
    listed = tmp_path / "omitted.txt"
    proc, _ = _run(
        tmp_path,
        [_record(filename="bin.webp", patch=None)],
        "--omitted-out",
        str(listed),
    )
    assert proc.returncode == 0, proc.stderr
    assert listed.read_text(encoding="utf-8") == "bin.webp\n"


def test_a_pr_whose_every_file_lacks_a_patch_is_empty_not_an_error(tmp_path):
    """Exit 0 with files_patched=0. The workflow decides what that means;
    failing here would turn "nothing reviewable" into a red check."""
    proc, out = _run(tmp_path, [_record(patch=None)])
    assert proc.returncode == 0, proc.stderr
    assert out.read_text(encoding="utf-8") == ""
    assert "files_patched=0" in proc.stdout


def test_malformed_input_exits_as_a_ci_fault(tmp_path):
    src = tmp_path / "files.jsonl"
    src.write_text("{not json\n", encoding="utf-8")
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--files",
            str(src),
            "--out",
            str(tmp_path / "out.txt"),
        ],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 2, proc.stdout + proc.stderr


def test_counts_go_to_github_output_when_given(tmp_path):
    target = tmp_path / "gh_out"
    target.write_text("", encoding="utf-8")
    proc, _ = _run(tmp_path, [_record()], "--github-output", str(target))
    assert proc.returncode == 0, proc.stderr
    written = target.read_text(encoding="utf-8")
    assert "files_total=1" in written
    assert "files_patched=1" in written


def test_github_output_is_appended_never_truncated(tmp_path):
    """Every step in the job shares that file.

    Opening it with "w" would discard whatever earlier steps had written,
    and the loss is silent — the job simply stops seeing outputs it set.
    """
    target = tmp_path / "gh_out"
    target.write_text("set_by_an_earlier_step=1\n", encoding="utf-8")
    proc, _ = _run(tmp_path, [_record()], "--github-output", str(target))
    assert proc.returncode == 0, proc.stderr
    written = target.read_text(encoding="utf-8")
    assert "set_by_an_earlier_step=1" in written
    assert "files_total=1" in written


def test_an_unwritable_output_is_a_named_ci_fault(tmp_path):
    """`guard` catches the OSError regardless; the point is that it says
    WHICH file, in a script that writes three of them."""
    proc, _ = _run(
        tmp_path,
        [_record()],
        "--github-output",
        str(tmp_path / "no_such_dir" / "gh_out"),
    )
    assert proc.returncode == 2, proc.stdout
    assert "gh_out" in proc.stdout, (
        "the fault does not name the file that could not be written"
    )


def test_an_unwritable_omitted_list_is_a_named_ci_fault(tmp_path):
    proc, _ = _run(
        tmp_path,
        [_record(patch=None)],
        "--omitted-out",
        str(tmp_path / "no_such_dir" / "omitted.txt"),
    )
    assert proc.returncode == 2, proc.stdout
    assert "omitted.txt" in proc.stdout


# -------------------------------------------- the pipeline's own consumers


def test_the_rebuilt_diff_survives_the_filter_it_is_fed_to():
    """build -> prepare_review_diff is the actual wiring in the workflow.

    split_sections returns an unrecognised diff whole as the PREAMBLE, so a
    header this script got subtly wrong would not raise — it would quietly
    produce zero reviewable sections.
    """
    import prepare_review_diff

    diff, _ = m.build(
        [
            _record(filename="src/app.py"),
            _record(filename="uv.lock", patch="@@ -1 +1 @@\n-a\n+b"),
        ]
    )
    preamble, sections = prepare_review_diff.split_sections(diff)
    assert preamble in ([], [""]), f"unparsed rows fell through: {preamble}"
    assert len(sections) == 2

    filtered, _stats = prepare_review_diff.filter_diff(diff)
    assert "src/app.py" in filtered
    assert "uv.lock" not in filtered, "the lockfile filter stopped matching"
