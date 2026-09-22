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

"""Deterministic tests for the fuzzy-match replacer ladder.

Pure string logic — no environment, no async, no LLM. Each replacer is a
generator of candidate search strings that already occur verbatim in the
content; ``find_replacement`` walks them in order applying the
uniqueness / replace_all rule.
"""

from __future__ import annotations

from horizon.tools._replacers import (
    block_anchor_replacer,
    find_replacement,
    indentation_flexible_replacer,
    line_trimmed_replacer,
    simple_replacer,
    whitespace_normalized_replacer,
)


def _yields(replacer, content: str, find: str) -> list[str]:
    return list(replacer(content, find))


class TestSimpleReplacer:
    def test_yields_find_verbatim(self) -> None:
        assert _yields(simple_replacer, "abc", "b") == ["b"]


class TestLineTrimmedReplacer:
    def test_matches_ignoring_leading_trailing_whitespace(self) -> None:
        content = "def f():\n      return 1\n"
        # model supplied the line with the wrong indent
        find = "    return 1"
        candidates = _yields(line_trimmed_replacer, content, find)
        assert "      return 1" in candidates

    def test_multi_line_block(self) -> None:
        content = "a = 1\n   b = 2  \nc = 3\n"
        find = "a = 1\nb = 2"
        candidates = _yields(line_trimmed_replacer, content, find)
        assert any("b = 2" in c for c in candidates)

    def test_no_match_yields_nothing(self) -> None:
        assert _yields(line_trimmed_replacer, "a = 1\n", "zzz") == []


class TestWhitespaceNormalizedReplacer:
    def test_collapses_internal_whitespace_runs(self) -> None:
        content = "x   =        1\n"
        find = "x = 1"
        candidates = _yields(whitespace_normalized_replacer, content, find)
        assert "x   =        1" in candidates


class TestIndentationFlexibleReplacer:
    def test_matches_ignoring_common_indentation(self) -> None:
        content = "class C:\n    def m(self):\n        return 1\n"
        # model dedented the whole block
        find = "def m(self):\n    return 1"
        candidates = _yields(indentation_flexible_replacer, content, find)
        assert any("def m(self):" in c and "return 1" in c for c in candidates)


class TestBlockAnchorReplacer:
    def test_matches_block_by_first_and_last_line_when_interior_drifts(
        self,
    ) -> None:
        content = "def f():\n    a = 1\n    b = 2\n    return a + b\n"
        # interior line differs ("a = 99"); anchors (first/last) still match
        find = "def f():\n    a = 99\n    return a + b"
        candidates = _yields(block_anchor_replacer, content, find)
        assert len(candidates) == 1
        assert candidates[0].startswith("def f():")
        assert candidates[0].rstrip().endswith("return a + b")

    def test_requires_at_least_three_lines(self) -> None:
        content = "def f():\n    return 1\n"
        assert (
            _yields(block_anchor_replacer, content, "def f():\n    return 1")
            == []
        )


class TestFindReplacement:
    # There is no replace_all mode: the only caller (file_ops._resolve_edits)
    # resolves one edits[] item at a time and always wants exactly one match;
    # batching N occurrences means passing N edits, not a replace_all flag.
    def test_exact_unique_match(self) -> None:
        res = find_replacement('print("hi")\n', 'print("hi")')
        assert res.error is None
        assert res.search == 'print("hi")'
        assert res.count == 1

    def test_not_found_returns_error_listing_strategies(self) -> None:
        res = find_replacement("a = 1\n", "nowhere")
        assert res.search is None
        assert res.error is not None
        # error names what was tried so the model can react
        assert "simple" in res.error.lower() or "tried" in res.error.lower()

    def test_ambiguous_exact_match_errors(self) -> None:
        res = find_replacement("x = 1\nx = 1\n", "x = 1")
        assert res.search is None
        assert res.error is not None
        assert (
            "multiple" in res.error.lower()
            or "ambig" in res.error.lower()
            or "unique" in res.error.lower()
        )

    def test_fuzzy_fallback_when_indentation_drifts(self) -> None:
        content = "def f():\n        return 1\n"
        res = find_replacement(content, "def f():\n    return 1")
        assert res.error is None
        # resolved to the real (over-indented) text in the file
        assert "return 1" in res.search
        assert res.count == 1

    def test_tighter_replacer_wins_over_looser(self) -> None:
        # exact match exists; must be chosen by simple_replacer, count 1
        content = "alpha\nbeta\ngamma\n"
        res = find_replacement(content, "beta")
        assert res.search == "beta"
        assert res.count == 1
