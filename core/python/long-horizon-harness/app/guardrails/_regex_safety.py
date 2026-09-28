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

"""Heuristic ReDoS guard for tenant-authored regexes.

Python's ``re`` has no match timeout, so a malicious/buggy pattern in a
``.lha/*.jsonl`` overlay could hang a request. This is a static heuristic
(length + nested-quantifier shapes), not a proof — callers skip patterns it
rejects rather than compiling them.
"""

from __future__ import annotations

import re

_MAX_LEN = 1000
# A quantified group whose body itself ends in a quantifier: (…+)+, (.*)*, ([a-z]+)+
_NESTED_QUANTIFIER = re.compile(r"\([^)]*[+*][^)]*\)\s*[+*{]")


def safe_regex(pattern: str) -> bool:
    if not isinstance(pattern, str) or len(pattern) > _MAX_LEN:
        return False
    return _NESTED_QUANTIFIER.search(pattern) is None


__all__ = ["safe_regex"]
