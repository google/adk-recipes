"""Escaping regressions. All three of these silently corrupted a real report."""

import json
import subprocess
import sys
from pathlib import Path

import build_report as br

SCRIPT = Path(__file__).parent.parent / "scripts" / "build_report.py"


def run(tmp_path, candidates, **flags):
    cj = tmp_path / "c.json"
    cj.write_text(json.dumps(candidates))
    md = tmp_path / "r.md"
    args = [
        sys.executable,
        str(SCRIPT),
        "--candidates",
        str(cj),
        "--repo",
        "o/r",
        "--pr",
        "1",
        "--out-md",
        str(md),
    ]
    for k, v in flags.items():
        args += [f"--{k.replace('_', '-')}", str(v)]
    proc = subprocess.run(args, capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr
    return md.read_text()


def cand(**kw):
    base = {
        "path": "a/b.py",
        "line": 3,
        "severity": "no_critical",
        "comment": "c",
        "window": "   3: x = 1",
        "verify_steps": "read line 3",
    }
    base.update(kw)
    return base


# ------------------------------------------------------------- real bugs


def test_pipe_in_source_does_not_split_the_row(tmp_path):
    """A raw | in a code window silently ate the rest of the table row."""
    md = run(tmp_path, [cand(window="   3: x = a | b")])
    rows = [ln for ln in md.split("\n") if ln.startswith("| 1 ")]
    assert rows and "&#124;" in rows[0]
    assert rows[0].count("|") - rows[0].count("\\|") == 7


def test_backticks_in_source_do_not_open_a_fence(tmp_path):
    """RST double-backticks in test_web_search.py produced a stray ``` fence
    that swallowed the remainder of the document."""
    md = run(tmp_path, [cand(window="   3: * see ``root_agent.tools``")])
    assert md.count("```") == 0  # no fences at all now


def test_triple_backtick_in_source_is_neutralised(tmp_path):
    md = run(tmp_path, [cand(window='   3: t.replace("```json", "")')])
    assert md.count("```") == 0


def test_angle_brackets_are_escaped(tmp_path):
    """A raw < opens an HTML tag and eats everything after it."""
    md = run(tmp_path, [cand(window="   3: if a < b and c > d:")])
    assert "&lt;" in md and "&gt;" in md


# --------------------------------------------------------------- behaviour


def test_unanchorable_leaves_the_cheap_split(tmp_path):
    md = run(
        tmp_path,
        [
            cand(),
            cand(
                line=4,
                anchorable=False,
                ci="fail",
                rule="H1",
                window="   4: y = 2",
            ),
        ],
    )
    assert "## Un-anchorable" in md
    assert "**FAIL**" in md


def test_ci_failures_sort_before_advisory(tmp_path):
    md = run(
        tmp_path,
        [
            cand(
                line=3,
                anchorable=False,
                ci="advisory",
                rule="H9",
                what="adv one",
            ),
            cand(
                line=4,
                anchorable=False,
                ci="fail",
                rule="H1",
                what="fail one",
                window="   4: y = 2",
            ),
        ],
    )
    ua = md[md.index("## Un-anchorable") :]
    assert ua.index("fail one") < ua.index("adv one")


def test_no_csv_is_written_by_default(tmp_path):
    """The CSV duplicated the markdown tables; only the report lands now."""
    run(tmp_path, [cand()])
    assert list(tmp_path.glob("*.csv")) == []


def test_criticals_sort_first(tmp_path):
    md = run(
        tmp_path,
        [
            cand(
                path="a/z.py", line=9, comment="minor one", window="   9: z = 1"
            ),
            cand(path="a/a.py", severity="critical", comment="serious one"),
        ],
    )
    body = md[md.index("## Comments") :]
    assert body.index("serious one") < body.index("minor one")


def test_report_is_all_tables_no_prose_sections(tmp_path):
    """The user asked for tables only; detail sections used to be H3 + fences."""
    md = run(tmp_path, [cand()])
    assert "### " not in md
    assert md.count("```") == 0


# ------------------------------------------------------------------- units


def test_anchor_line_extracts_the_right_line():
    assert br.anchor_line("   1: aaa\n   2: bbb\n   3: ccc", 2) == "bbb"


def test_anchor_line_falls_back_when_unnumbered():
    assert br.anchor_line("just some text", 5) == "just some text"


def test_common_prefix_strips_shared_directories():
    assert br.common_prefix(["x/y/a.py", "x/y/b.py"]) == "x/y/"


def test_common_prefix_empty_when_nothing_shared():
    assert br.common_prefix(["a/x.py", "b/y.py"]) == ""


def test_a_candidate_without_a_line_does_not_kill_the_report(tmp_path):
    """Candidates are model-authored JSON. A lane that omitted `line` raised
    KeyError inside the sort comparator, six frames from the cause, and the
    whole review -- every other lane's findings included -- was lost."""
    c = cand()
    del c["line"]
    md = run(tmp_path, [c, cand(path="a/c.py", line=9)])
    # Both findings survive, and the one with no line reports 0 rather than
    # taking the other one down with it.
    assert "`b.py`" in md and "`c.py`" in md
    assert "**Comments** 2" in md


def test_a_non_numeric_line_is_coerced_rather_than_crashing(tmp_path):
    md = run(tmp_path, [cand(line="not a number"), cand(line=None)])
    assert "**Comments** 2" in md


def test_csv_defuses_a_leading_formula(tmp_path):
    """The CSV is opened in a spreadsheet, and every string in it comes from
    the PR's own diff. A source line starting with = ran as a formula."""
    csv_path = tmp_path / "out.csv"
    run(
        tmp_path,
        [cand(comment="=cmd|'/c calc'!A1", window="   3: =HYPERLINK(x)")],
        out_csv=csv_path,
    )
    text = csv_path.read_text(encoding="utf-8")
    assert '"\t=cmd' in text, "formula comment was not defused"
    assert "=cmd" in text, "the original text must survive, just inert"


def test_csv_leaves_ordinary_values_alone(tmp_path):
    csv_path = tmp_path / "out.csv"
    run(tmp_path, [cand(comment="plain text")], out_csv=csv_path)
    text = csv_path.read_text(encoding="utf-8")
    assert '"plain text"' in text
    assert "\tplain" not in text


def test_csv_safe_covers_every_formula_lead():
    for lead in ("=", "+", "-", "@"):
        assert br.csv_safe(f"{lead}x") == f"\t{lead}x"
    assert br.csv_safe("x=1") == "x=1"
    assert br.csv_safe(None) == ""


def test_report_is_written_as_utf8_regardless_of_locale(tmp_path):
    """truncate() emits U+2026. Under a non-UTF-8 locale a bare open(..., "w")
    picks ASCII, raises UnicodeEncodeError on that character, and the run ends
    with no report at all -- every finding lost to the reviewer's LANG."""
    import os

    cj = tmp_path / "c.json"
    cj.write_text(json.dumps([cand(window="   3: " + "x" * 400)]))
    md = tmp_path / "r.md"
    csv_out = tmp_path / "r.csv"
    env = {
        **os.environ,
        "LC_ALL": "C",
        "LANG": "C",
        "PYTHONUTF8": "0",
        "PYTHONCOERCECLOCALE": "0",
    }
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--candidates",
            str(cj),
            "--repo",
            "o/r",
            "--pr",
            "1",
            "--out-md",
            str(md),
            "--out-csv",
            str(csv_out),
        ],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    assert proc.returncode == 0, proc.stderr
    assert "…" in md.read_text(encoding="utf-8")
    assert csv_out.exists()
