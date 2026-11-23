# ruff: noqa
# fmt: off
# mypy: ignore-errors
# Copyright (c) Django Software Foundation and individual contributors.
# All rights reserved. Licensed under the BSD 3-Clause License; see
# ../../LICENSE. This third-party file intentionally keeps Django's licence
# rather than the Apache header used by the rest of the recipe.
# Excerpt of django/core/exceptions.py from Django
# (https://github.com/django/django) at commit
# 6822aa5c6c3fbec7c5393a05e990865ba59fe167: only the class the CVE-2021-3281
# fix imports. BSD-3-Clause, see ../../LICENSE.
"""
Global Django exception and warning classes.
"""


class SuspiciousOperation(Exception):
    """The user did something suspicious"""
