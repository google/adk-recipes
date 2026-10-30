# Bundled target: Django `archive.extract()` path traversal

This directory is a minimal, unmodified excerpt of Django, used as the
default target of the code security fixer. It reproduces two published,
related vulnerabilities in `django/utils/archive.py`.

| | CVE-2021-3281 | CVE-2025-59682 |
| --- | --- | --- |
| Advisory | [GHSA-fvgf-6h6h-3322](https://github.com/advisories/GHSA-fvgf-6h6h-3322) | [GHSA-q95w-c7qg-hrff](https://github.com/advisories/GHSA-q95w-c7qg-hrff) |
| Issue | Archives with absolute paths or `..` segments are extracted outside the target directory | The 2021 fix used `str.startswith`, so a sibling directory sharing the prefix (`<target>abc/`) still escaped |
| Historical fix | [`05413afa`](https://github.com/django/django/commit/05413afa8c18cdb978fcdf470e09f7a12b234a23) | [`924a0c09`](https://github.com/django/django/commit/924a0c092e65fa2d0953fd1855d2dc8786d94de2) |

## Provenance

- Source: https://github.com/django/django
- Commit: `6822aa5c6c3fbec7c5393a05e990865ba59fe167` (the parent of the
  CVE-2021-3281 fix)
- `django/utils/archive.py`: byte-identical to upstream below a short
  vendoring header (lint/format/type-check directives and this provenance).
  The module is itself based on python-archive (MIT, notice in its
  docstring).
- `django/core/exceptions.py`: only the `SuspiciousOperation` class, which
  the historical fixes import.
- Licence: BSD-3-Clause, see [`LICENSE`](LICENSE) (copied from the same
  commit).

## Tests

- `tests/test_archive.py`: Django's functional tests for `extract()` at the
  same commit, adapted to build the fixture archives in code instead of
  vendoring binaries. They must keep passing.
- `tests/test_traversal.py`: exploit tests reproducing the archives from
  Django's regression tests for both CVEs, aimed inside a private temporary
  directory. They fail on this code by design.

Never use this code outside the recipe.
