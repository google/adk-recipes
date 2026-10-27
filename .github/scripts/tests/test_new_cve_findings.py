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
"""Unit tests for new_cve_findings.py.

Reports are shaped like osv-scanner v2.6.0 `--format json` output
(pkg/models/results.go): results[].source.path is absolute, and each
package carries `vulnerabilities` plus `groups` of ids and aliases.
"""

import json

import new_cve_findings as n
import pytest

LOCK = "core/python/genmedia-for-commerce/uv.lock"


def pkg(name, version, *groups):
    return {
        "package": {"name": name, "version": version, "ecosystem": "PyPI"},
        "vulnerabilities": [{"id": i} for g in groups for i in g[0]],
        "groups": [
            {"ids": list(ids), "aliases": list(aliases), "max_severity": ""}
            for ids, aliases in groups
        ],
    }


def report(root, *packages, lockfile=LOCK):
    return {
        "results": [
            {
                "source": {"path": f"{root}/{lockfile}", "type": "lockfile"},
                "packages": list(packages),
            }
        ]
    }


def group(*ids, aliases=()):
    return (ids, aliases)


def new(head, base, head_root="/w", base_root="/b"):
    return n.new_findings(
        n.findings(head, head_root), n.findings(base, base_root)
    )


def test_pr_2702_every_finding_already_on_main():
    # oauthlib and virtualenv unchanged; litellm bumped from a version with
    # six advisories to one with a single advisory it shares with main.
    base = report(
        "/b",
        pkg(
            "litellm",
            "1.83.14",
            group("GHSA-3cv6-jpf6-8222"),
            group("PYSEC-2026-388"),
        ),
        pkg("oauthlib", "3.3.1", group("GHSA-hj66-6f7g-4r5v")),
        pkg("virtualenv", "21.3.0", group("PYSEC-2026-4013")),
    )
    head = report(
        "/w",
        pkg("litellm", "1.85.7", group("GHSA-3cv6-jpf6-8222")),
        pkg("oauthlib", "3.3.1", group("GHSA-hj66-6f7g-4r5v")),
        pkg("virtualenv", "21.3.0", group("PYSEC-2026-4013")),
    )
    assert new(head, base) == []


def test_a_newly_added_vulnerable_package_is_new():
    base = report("/b", pkg("oauthlib", "3.3.1", group("GHSA-hj66-6f7g-4r5v")))
    head = report(
        "/w",
        pkg("oauthlib", "3.3.1", group("GHSA-hj66-6f7g-4r5v")),
        pkg("requests", "2.0.0", group("GHSA-new")),
    )
    assert [f.package for f in new(head, base)] == ["requests"]


def test_a_bump_onto_a_different_advisory_is_new():
    base = report("/b", pkg("litellm", "1.83.14", group("GHSA-old")))
    head = report("/w", pkg("litellm", "1.90.0", group("GHSA-other")))
    assert [f.primary_id for f in new(head, base)] == ["GHSA-other"]


def test_the_same_advisory_under_a_different_id_is_not_new():
    base = report("/b", pkg("x", "1", group("PYSEC-1", aliases=["CVE-1"])))
    head = report("/w", pkg("x", "1", group("GHSA-1", aliases=["CVE-1"])))
    assert new(head, base) == []


def test_the_same_advisory_in_another_lockfile_is_new_here():
    base = report("/b", pkg("x", "1", group("GHSA-1")), lockfile="a/uv.lock")
    head = report("/w", pkg("x", "1", group("GHSA-1")), lockfile="b/uv.lock")
    assert len(new(head, base)) == 1


def test_a_lockfile_new_in_the_pr_has_only_new_findings():
    head = report("/w", pkg("x", "1", group("GHSA-1")))
    assert len(n.new_findings(n.findings(head, "/w"), [])) == 1


def test_ungrouped_vulnerabilities_fall_back_to_ids_and_aliases():
    base = report("/b", pkg("x", "1"))
    base["results"][0]["packages"][0]["vulnerabilities"] = [
        {"id": "PYSEC-1", "aliases": ["GHSA-1"]}
    ]
    head = report("/w", pkg("x", "1"))
    head["results"][0]["packages"][0]["vulnerabilities"] = [{"id": "GHSA-1"}]
    assert new(head, base) == []


def test_paths_are_compared_relative_to_each_root():
    found = n.findings(
        report("/tmp/runner/cve_base", pkg("x", "1", group("A"))),
        "/tmp/runner/cve_base",
    )
    assert found[0].lockfile == LOCK


# --- command line ---------------------------------------------------------


def write(tmp_path, name, data):
    path = tmp_path / name
    path.write_text(json.dumps(data) if not isinstance(data, str) else data)
    return str(path)


def run(monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["new_cve_findings.py", *argv])
    return n.main()


def test_exit_0_when_nothing_is_new(tmp_path, monkeypatch, capsys):
    r = pkg("x", "1", group("A"))
    head = write(tmp_path, "h.json", report(str(tmp_path / "w"), r))
    base = write(tmp_path, "b.json", report(str(tmp_path / "b"), r))
    code = run(
        monkeypatch,
        "--head",
        head,
        "--head-root",
        str(tmp_path / "w"),
        "--base",
        base,
        "--base-root",
        str(tmp_path / "b"),
    )
    assert code == 0
    assert "already exist on the base branch" in capsys.readouterr().out


def test_exit_1_lists_what_is_new(tmp_path, monkeypatch, capsys):
    head = write(
        tmp_path,
        "h.json",
        report(str(tmp_path / "w"), pkg("x", "1", group("GHSA-9"))),
    )
    code = run(monkeypatch, "--head", head, "--head-root", str(tmp_path / "w"))
    assert code == 1
    assert "https://osv.dev/GHSA-9" in capsys.readouterr().out


@pytest.mark.parametrize(
    "bad", ["not json", '{"results": [{"source": {}}]}', "null", "[]"]
)
def test_an_unreadable_report_is_exit_2_never_a_pass(
    tmp_path, monkeypatch, capsys, bad
):
    head = write(tmp_path, "h.json", bad)
    assert run(monkeypatch, "--head", head, "--head-root", str(tmp_path)) == 2
    # Reported as a CI fault, never pointed at the contributor's lockfile.
    assert "NOT caused by your changes" in capsys.readouterr().out


def test_base_without_base_root_is_a_usage_error(monkeypatch):
    with pytest.raises(SystemExit):
        run(monkeypatch, "--head", "h", "--head-root", ".", "--base", "b")


@pytest.mark.parametrize(("groups", "expected"), [([], "0"), (["GHSA-9"], "1")])
def test_new_count_is_written_on_completion(
    tmp_path, monkeypatch, groups, expected
):
    packages = [pkg("x", "1", group(*groups))] if groups else []
    head = write(tmp_path, "h.json", report(str(tmp_path / "w"), *packages))
    count = tmp_path / "count"
    run(
        monkeypatch,
        "--head",
        head,
        "--head-root",
        str(tmp_path / "w"),
        "--new-count",
        str(count),
    )
    assert count.read_text().strip() == expected


def test_new_count_is_not_written_when_a_report_is_unreadable(
    tmp_path, monkeypatch
):
    head = write(tmp_path, "h.json", "not json")
    count = tmp_path / "count"
    run(
        monkeypatch,
        "--head",
        head,
        "--head-root",
        str(tmp_path),
        "--new-count",
        str(count),
    )
    assert not count.exists()
