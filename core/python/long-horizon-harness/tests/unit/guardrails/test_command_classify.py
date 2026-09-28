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

"""Unit tests for shell command classification used by the permission gate."""

from __future__ import annotations

import pytest

from app.guardrails.command_classify import (
    command_prefix,
    has_command_substitution,
    has_redirection,
    significant_tokens,
    split_segments,
    strip_wrapper,
)


@pytest.mark.parametrize(
    "command,expected",
    [
        ("bash -c 'bq rm -t x.y'", "bq rm -t x.y"),
        ('sh -c "ls -la"', "ls -la"),
        ("ls -la", "ls -la"),
    ],
)
def test_strip_wrapper(command, expected):
    assert strip_wrapper(command) == expected


@pytest.mark.parametrize(
    "command,expected",
    [
        ("ls && bq rm x", ["ls", "bq rm x"]),
        ("a | b | c", ["a", "b", "c"]),
        ("x; y || z", ["x", "y", "z"]),
        ("single", ["single"]),
        ("ls & bq rm x", ["ls", "bq rm x"]),  # bare & (background)
        ("ls\nbq rm x", ["ls", "bq rm x"]),  # newline
        ("a && b", ["a", "b"]),  # && still splits to 2, not on the inner &
        ("cmd 2>&1", ["cmd 2>&1"]),  # fd-dup & is not a separator
        ("gws x 2>&1 | head", ["gws x 2>&1", "head"]),
        ("cmd &>out", ["cmd &>out"]),  # &> redirect, not background
        # Operators inside quotes are literal — a quoted body stays one segment.
        ('echo "a; b | c"', ['echo "a; b | c"']),
        ("echo 'a && b'", ["echo 'a && b'"]),
        ('python3 -c "x; y\nz"', ['python3 -c "x; y\nz"']),  # multiline -c body
        # ...but operators OUTSIDE the quotes still split.
        ('python3 -c "x; y" ; ls', ['python3 -c "x; y"', "ls"]),
    ],
)
def test_split_segments(command, expected):
    assert split_segments(command) == expected


@pytest.mark.parametrize(
    "segment,expected",
    [
        ("ls $(bq rm x)", True),
        ("echo `bq rm x`", True),
        ("diff <(a) <(b)", True),
        ("ls -la", False),
        # Single quotes suppress every expansion, so a literal backtick (the
        # BigQuery table-ref quoting) is not command substitution.
        ("bq query 'SELECT 1 FROM `proj.ds.tbl`'", False),
        ("echo 'costs $(a lot)'", False),
        ("echo 'diff <(a)'", False),
        # ...but bash DOES expand $() and backticks inside double quotes.
        ('bq query "SELECT 1 FROM `proj.ds.tbl`"', True),
        ('echo "today is $(date)"', True),
        ('echo "escaped \\$(date)"', False),  # backslash-escaped, no expansion
        # Process substitution is not performed inside double quotes.
        ('echo "<(a)"', False),
        # ANSI-C quoting: bash DOES process backslash escapes inside $'…', so
        # $'\'' is one literal quote char. Scanning it as a plain single-quoted
        # run desyncs quote state and hides the substitution that follows.
        (r"""bq query $'\'' $(cat /etc/passwd)""", True),
        (r"""echo $'a\'b' $(id)""", True),
        (r"""echo $'\'' `id`""", True),
        (r"echo $'plain text'", False),  # ANSI-C with nothing nested
        (r"echo $'tab\there' $(id)", True),
        # Unbalanced quoting can't be reasoned about — fail closed.
        ('echo "unterminated', True),
        ("echo 'unterminated", True),
        (r"echo $'unterminated", True),
    ],
)
def test_has_command_substitution(segment, expected):
    assert has_command_substitution(segment) is expected


@pytest.mark.parametrize(
    "segment,expected",
    [
        ("bq rm -t x.y", "bq rm"),
        ("git push origin main", "git push"),
        ("npm install foo", "npm install"),
        ("python train.py", "python"),  # train.py is not a bare word → stop
        ("ls", "ls"),
        ("FOO=bar bq rm x", "bq rm"),  # skip env assignment
        # sudo is NOT unwrapped: the prefix must name the escalation, or a plain
        # `npm install` grant would flag-tolerantly cover `sudo npm install`.
        ("sudo systemctl restart x", "sudo systemctl"),
        ("./run.sh arg", "./run.sh"),  # binary with no bare-word subcmd
        # Self-contained --flag=value tokens are skipped, so the auto-scope prefix
        # doesn't collapse to the whole binary (which would grant `bq rm` off the
        # back of approving a harmless `bq query`).
        ("bq --project_id=p query 'SELECT 1'", "bq query"),
        ("gcloud --project=p compute instances list", "gcloud compute"),
        ("ls -la", "ls"),  # flag-only tail still yields the bare binary
        # A BARE flag may consume the next token as its value, and nothing here
        # knows which CLI does that — so scanning stops rather than mistake the
        # value for a subcommand. Honest-broad beats confidently-wrong.
        ("kubectl --context=p -n default delete pod x", "kubectl"),
        ("gcloud --quiet compute instances list", "gcloud"),
        ("docker -H tcp://h run img", "docker"),
    ],
)
def test_command_prefix(segment, expected):
    assert command_prefix(segment) == expected


@pytest.mark.parametrize(
    "segment,expected",
    [
        ("echo hi > out.txt", True),
        ("cat a >> b", True),
        ("ls -la", False),
        ("cmd 2> err.log", True),
        ("cmd 1>&2", False),  # fd-dup, not a file write
        ("cmd 2>&1", False),
        (
            "cmd 2>&1 > out.txt",
            True,
        ),  # fd-dup + real redirect → still redirects
        ("cmd &>out", True),  # &> opens a file
        ("cmd 2>/dev/null", False),  # /dev/null is a sink, not a file write
        ("cmd >/dev/null", False),
        ("cmd > /dev/null", False),  # spaced
        ("cmd >>/dev/null 2>&1", False),  # append-to-null + fd-dup
        ("cmd &>/dev/null", False),
        (
            "cmd 2>/dev/null > out.txt",
            True,
        ),  # null sink + real redirect → still redirects
        (
            "cmd > /dev/null2",
            True,
        ),  # a real file that merely starts with /dev/null
        ("cmd > /dev/null.txt", True),  # a sibling file in /dev, not the sink
        ("cmd > /dev/null/../etc/foo", True),  # traversal off the device path
    ],
)
def test_has_redirection(segment, expected):
    assert has_redirection(segment) is expected


@pytest.mark.parametrize(
    "segment,expected",
    [
        ("bq --project_id=X query t", ["bq", "query", "t"]),
        ("bq rm -t x.y", ["bq", "rm"]),  # bare flag ends the walk
        ("kubectl -n get delete pod", ["kubectl"]),  # value can't impersonate
        ("sudo npm install x", ["sudo", "npm", "install", "x"]),  # never unwrap
        ("env FOO=1 bq rm x", ["bq", "rm", "x"]),
        ("cmd --a=b=c --out=-x sub", ["cmd", "sub"]),  # self-contained flags
        ("cmd --flag= sub", ["cmd", "sub"]),  # empty value is still contained
        ("gcloud -- compute rm", ["gcloud"]),  # `--` is bare → stop
        ("tail -1 f", ["tail"]),
        ("", []),
    ],
)
def test_significant_tokens(segment, expected):
    assert significant_tokens(segment) == expected


def test_derived_prefix_always_matches_its_own_command():
    """The shared token walk's core invariant: a prefix derived from a command
    must match that command, or approving it re-prompts forever."""
    import itertools

    from app.guardrails.permission_rules import _prefix_matches

    parts = ["sudo", "env", "FOO=b", "bq", "./r.sh", "--q", "-n", "--p=v", "--"]
    words = ["query", "rm", "default", "x.y"]
    violations = [
        cmd
        for combo in itertools.product(parts + words, repeat=3)
        for cmd in [" ".join(combo)]
        if not _prefix_matches(cmd, command_prefix(cmd))
    ]
    assert violations == []
