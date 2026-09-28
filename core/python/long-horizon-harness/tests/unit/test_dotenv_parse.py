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

from app.secrets.dotenv import ParsedDotenv, Skipped, parse_dotenv


def test_basic_key_values():
    result = parse_dotenv("FOO=bar\nBAZ=qux\n")
    assert result.entries == {"FOO": "bar", "BAZ": "qux"}
    assert result.skipped == []


def test_export_prefix_is_stripped():
    assert parse_dotenv("export FOO=bar").entries == {"FOO": "bar"}


def test_blank_lines_and_comments_ignored():
    text = "\n# a comment\n   # indented comment\nFOO=bar\n"
    result = parse_dotenv(text)
    assert result.entries == {"FOO": "bar"}
    assert result.skipped == []


def test_double_quoted_value_unquoted():
    assert parse_dotenv('FOO="bar baz"').entries == {"FOO": "bar baz"}


def test_single_quoted_value_unquoted():
    assert parse_dotenv("FOO='bar baz'").entries == {"FOO": "bar baz"}


def test_inline_comment_stripped_only_when_unquoted():
    assert parse_dotenv("FOO=bar # trailing").entries == {"FOO": "bar"}
    assert parse_dotenv('FOO="bar # not a comment"').entries == {
        "FOO": "bar # not a comment"
    }


def test_value_with_equals_splits_on_first():
    assert parse_dotenv("URL=postgres://a=b").entries == {
        "URL": "postgres://a=b"
    }


def test_line_without_equals_is_skipped():
    result = parse_dotenv("FOO=bar\njust a line\n")
    assert result.entries == {"FOO": "bar"}
    assert result.skipped == [Skipped(lineno=2, reason="no '='")]


def test_invalid_key_name_is_skipped():
    result = parse_dotenv("9FOO=bar\nGOOD=1\nbad-key=2\n")
    assert result.entries == {"GOOD": "1"}
    assert result.skipped == [
        Skipped(lineno=1, reason="invalid key name"),
        Skipped(lineno=3, reason="invalid key name"),
    ]


def test_crlf_line_endings():
    assert parse_dotenv("FOO=bar\r\nBAZ=qux\r\n").entries == {
        "FOO": "bar",
        "BAZ": "qux",
    }


def test_duplicate_key_last_wins():
    assert parse_dotenv("FOO=1\nFOO=2\n").entries == {"FOO": "2"}


def test_empty_input():
    result = parse_dotenv("")
    assert result == ParsedDotenv(entries={}, skipped=[])


def test_empty_value_is_allowed():
    assert parse_dotenv("FOO=").entries == {"FOO": ""}


def test_value_that_is_only_a_comment_is_empty():
    assert parse_dotenv("FOO= # just a comment").entries == {"FOO": ""}


def test_key_surrounding_whitespace_is_trimmed():
    assert parse_dotenv("  FOO  =bar").entries == {"FOO": "bar"}


def test_unquoted_value_is_trimmed_but_quoted_inner_spaces_preserved():
    assert parse_dotenv("FOO=  bar  ").entries == {"FOO": "bar"}
    assert parse_dotenv('FOO="  bar  "').entries == {"FOO": "  bar  "}


def test_lone_quote_is_literal_value():
    assert parse_dotenv('FOO="').entries == {"FOO": '"'}
