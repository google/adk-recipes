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

"""Compact declarations are ours alone, and every param keeps a type."""

from __future__ import annotations

from typing import Any

from google.adk.features._feature_registry import (
    FeatureName,
    is_feature_enabled,
)
from google.adk.tools.function_tool import FunctionTool

from horizon.context.declaration_compaction import compact_tool_declarations


def _sample(query: str | None = None) -> str:
    """Somebody else's tool."""
    return ""


def _is_compact(tool: Any) -> bool:
    # .parameters is the compact rendering; JSON Schema leaves it None.
    return tool._get_declaration().parameters is not None


def test_ours_compact_theirs_untouched() -> None:
    (ours,) = compact_tool_declarations([_sample])
    assert _is_compact(ours)
    assert not _is_compact(FunctionTool(_sample))


def test_a_toolset_passes_through() -> None:
    ts = object()
    assert compact_tool_declarations([ts])[0] is ts


def test_importing_horizon_leaves_the_process_default_alone() -> None:
    import horizon.agent  # noqa: F401

    # Catches the env var and override_feature_enabled alike: either would
    # re-render the declarations of every other ADK agent in the process.
    assert is_feature_enabled(FeatureName.JSON_SCHEMA_FOR_FUNC_DECL)


def _typeless(node: Any, path: str) -> list[str]:
    if not isinstance(node, dict):
        return []
    bad = [path] if path and not (node.get("type") or node.get("enum")) else []
    kids = list((node.get("properties") or {}).items())
    kids += [("[]", node["items"])] if "items" in node else []
    return bad + [p for k, v in kids for p in _typeless(v, f"{path}.{k}")]


def test_no_param_reaches_vertex_without_a_type() -> None:
    # Compact rendering drops anyOf, so a param annotated `Any` becomes a
    # bare {"nullable": true} and Vertex 400s the whole request with
    # "didn't specify the schema type field".
    probe = {"type": "OBJECT", "properties": {"m": {"nullable": True}}}
    assert _typeless(probe, "p") == ["p.m"]

    from horizon.agent import root_agent

    offenders = []
    for tool in root_agent.tools:
        decl = getattr(tool, "_get_declaration", lambda: None)()
        if decl is None:
            continue
        params = decl.parameters or decl.parameters_json_schema or {}
        if not isinstance(params, dict):
            params = params.model_dump(exclude_none=True)
        offenders += _typeless(params, decl.name)
    assert not offenders, f"Vertex will reject: {offenders}"
