# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Keeps an optional variable's `validation` block evaluable on the oldest
Terraform the module claims, which `versions.tf` puts at 1.4.

Terraform evaluates both operands of `||` and `&&` before 1.12, so the usual
`var.x == null || var.x >= 1` guard does not protect the comparison: on 1.11
and earlier the plan aborts with "argument must not be null" for a variable the
operator never set. The short-circuiting forms are a ternary, which evaluates
only the branch it selects, and `can()`, which swallows the error.

Nothing in CI reaches this. `terraform validate` does not evaluate variable
validations, and the `terraform-check` workflow installs the latest release,
where `||` short-circuits and the broken form passes.
"""

from __future__ import annotations

import pathlib
import re

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
_TERRAFORM_DIR = _REPO_ROOT / "terraform"

# `default = null`, whatever the run of spaces buildifier leaves.
_NULL_DEFAULT = re.compile(r"^\s*default\s*=\s*null\s*$", re.MULTILINE)

# The unsafe guard: a null test joined to the rest of the condition by `||`.
_NULL_OR_GUARD = re.compile(r"==\s*null\s*\n?\s*\|\|(?P<rest>.*)", re.DOTALL)

# A right operand that is wholly one `can()` call is safe however the operands
# are evaluated, since `can()` returns false instead of raising.
_WHOLLY_CAN = re.compile(r"^\s*can\(.*\)\s*$", re.DOTALL)


def _variable_blocks(source: str) -> dict[str, str]:
    """Extracts the body of every variable block in a Terraform source file.

    Args:
        source: Terraform configuration file content.

    Returns:
        Mapping of variable names to their block body strings.
    """
    blocks: dict[str, str] = {}
    parts = source.split('variable "')
    for part in parts[1:]:
        name, _, body = part.partition('"')
        blocks[name] = body.split("\nvariable ")[0]
    return blocks


def _validation_conditions(body: str) -> list[str]:
    """Extracts condition expressions from validation blocks in a variable body.

    Validation conditions are terminated by `error_message`, which every block
    in this module declares after its condition.

    Args:
        body: Variable block body string.

    Returns:
        List of condition expression strings.
    """
    return [
        match.group("condition")
        for match in re.finditer(
            r"condition\s*=(?P<condition>.*?)\n\s*error_message",
            body,
            re.DOTALL,
        )
    ]


def _optional_variables() -> list[tuple[str, str, str]]:
    """Discovers every optional variable condition that a null value can reach.

    Returns:
        List of (file_name, variable_name, condition_expression) tuples.
    """
    cases: list[tuple[str, str, str]] = []
    for path in sorted(_TERRAFORM_DIR.rglob("variables.tf")):
        source = path.read_text(encoding="utf-8")
        for name, body in _variable_blocks(source).items():
            if not _NULL_DEFAULT.search(body):
                continue
            for condition in _validation_conditions(body):
                cases.append((path.name, name, condition))
    return cases


_OPTIONAL_VARIABLES = _optional_variables()


def test_the_module_declares_optional_variables_with_validation() -> None:
    """Guards the scan itself: a rename of `variables.tf`, or a parser that
    stops matching the file, would leave every test below vacuously green."""
    assert _OPTIONAL_VARIABLES, (
        "found no null-defaulted variable with a validation"
    )


@pytest.mark.parametrize(
    ("file_name", "variable", "condition"),
    _OPTIONAL_VARIABLES,
    ids=[f"{file}:{name}" for file, name, _ in _OPTIONAL_VARIABLES],
)
def test_an_optional_variable_short_circuits_its_null_guard(
    file_name: str, variable: str, condition: str
) -> None:
    guard = _NULL_OR_GUARD.search(condition)
    if guard is None:
        return

    assert _WHOLLY_CAN.match(guard.group("rest")), (
        f"{file_name}: {variable} guards null with `||`, which Terraform before "
        "1.12 evaluates on both sides -- the plan then fails on the null the "
        "guard is there to allow. Use `var.x == null ? true : (...)`, or wrap "
        "the right operand in `can()`."
    )


def _service_account_iam_resources() -> list[tuple[str, str, str]]:
    """Discovers service account IAM resource blocks across Terraform definitions.

    Returns:
        List of (file_name, resource_name, block_body) tuples.
    """
    cases: list[tuple[str, str, str]] = []
    pattern = re.compile(
        r'resource\s+"(google_service_account_iam_\w+)"\s+"(?P<name>\w+)"\s*\{(?P<body>.*?)\n\}',
        re.DOTALL,
    )
    for path in sorted(_TERRAFORM_DIR.rglob("*.tf")):
        source = path.read_text(encoding="utf-8")
        for match in pattern.finditer(source):
            cases.append((path.name, match.group("name"), match.group("body")))
    return cases


_SA_IAM_RESOURCES = _service_account_iam_resources()


def test_the_module_declares_service_account_iam_resources() -> None:
    assert _SA_IAM_RESOURCES, "found no google_service_account_iam_* resources"


@pytest.mark.parametrize(
    ("file_name", "resource_name", "body"),
    _SA_IAM_RESOURCES,
    ids=[f"{file}:{name}" for file, name, _ in _SA_IAM_RESOURCES],
)
def test_service_account_iam_resources_respect_act_as_grant_scope(
    file_name: str, resource_name: str, body: str
) -> None:
    """Every service-account IAM policy resource must be gated on
    `var.act_as_grant_scope == "service_account"`, so organisations that deny
    `iam.serviceAccounts.setIamPolicy` can switch to project-scoped bindings via
    `act_as_grant_scope = "project"`."""
    assert re.search(
        r'count\s*=\s*var\.act_as_grant_scope\s*==\s*"service_account"\s*\?\s*1\s*:\s*0',
        body,
    ), (
        f"{file_name}: {resource_name} is not gated on "
        '`var.act_as_grant_scope == "service_account"`. An organisation that '
        "denies `iam.serviceAccounts.setIamPolicy` will fail on apply even when "
        '`act_as_grant_scope = "project"` is set.'
    )


_DEFAULT_COMPUTE_SA = "-compute@developer.gserviceaccount.com"

# The `^` anchors skip commented-out blocks.
_RESOURCE_BLOCK = re.compile(
    r'^resource\s+"(?P<type>[\w-]+)"\s+"(?P<name>[\w-]+)"\s*\{(?P<body>.*?)^\}',
    re.MULTILINE | re.DOTALL,
)


def _list_resource_blocks(root: pathlib.Path) -> list[re.Match[str]]:
    """Lists the top-level resource blocks in the `.tf` files under `root`.

    Args:
        root: Directory searched recursively.

    Returns:
        Matches of `_RESOURCE_BLOCK`, in file then source order.
    """
    return [
        m
        for path in sorted(root.rglob("*.tf"))
        for m in _RESOURCE_BLOCK.finditer(path.read_text(encoding="utf-8"))
    ]


def test_bootstrap_grants_cloudbuild_builder_to_default_compute_service_account() -> (
    None
):
    """See the grant's comment in `terraform/bootstrap/iam.tf`."""
    member = (
        "serviceAccount:${data.google_project.this.number}"
        + _DEFAULT_COMPUTE_SA
    )
    blocks = [
        m.group("body")
        for m in _list_resource_blocks(_TERRAFORM_DIR / "bootstrap")
        if m.group("type") == "google_project_iam_member"
        and re.search(
            r'^\s*role\s*=\s*"roles/cloudbuild\.builds\.builder"\s*$',
            m.group("body"),
            re.MULTILINE,
        )
        and re.search(
            rf'^\s*member\s*=\s*"{re.escape(member)}"\s*$',
            m.group("body"),
            re.MULTILINE,
        )
    ]
    assert len(blocks) == 1, f"Grant the builder role to {member} once."
    assert not re.search(
        r"^\s*(count|for_each)\s*=", blocks[0], re.MULTILINE
    ), "The Cloud Build grant must be unconditional."
    assert re.search(
        r"^\s*depends_on\s*=\s*\[time_sleep\.propagation\]\s*$",
        blocks[0],
        re.MULTILINE,
    ), "Grant only after time_sleep.propagation, once the account exists."


def test_module_grants_nothing_to_default_compute_service_account() -> None:
    """See "Why this is a separate root" in `terraform/bootstrap/README.md`."""
    offenders = [
        f"{m.group('type')}.{m.group('name')}"
        for m in _list_resource_blocks(_TERRAFORM_DIR / "modules" / "aqa")
        if _DEFAULT_COMPUTE_SA in m.group("body")
    ]
    assert not offenders, f"Move to terraform/bootstrap: {offenders}"


def test_bootstrap_reads_the_project_after_enabling_resource_manager() -> None:
    """See the comment on `data.google_project.this` in `iam.tf`."""
    iam_tf = (_TERRAFORM_DIR / "bootstrap" / "iam.tf").read_text(
        encoding="utf-8"
    )
    data = re.search(
        r'^data\s+"google_project"\s+"this"\s*\{(?P<body>.*?)^\}',
        iam_tf,
        re.MULTILINE | re.DOTALL,
    )
    assert data is not None, "Declare data.google_project.this in iam.tf."
    assert re.search(
        r"^\s*depends_on\s*=\s*\[google_project_service\.bootstrap\]\s*$",
        data.group("body"),
        re.MULTILINE,
    ), "Read the project only after enabling Cloud Resource Manager."
