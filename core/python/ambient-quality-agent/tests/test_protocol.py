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

"""Pins each route's request `TypedDict` to the keys the agent's handler reads.

The handlers read the body as a plain dict, so nothing else notices a key one
side adds or renames. Reads are found in the handler's source, and in the
module's helpers it calls: `<x>.get("key")`, `<x>["key"]` and
`_extract_required_field(<x>, "key")`.
"""

from __future__ import annotations

import ast
import inspect

import pytest
from ambient_quality_agent.core import command_routes
from ambient_quality_shared import protocol

_FunctionDef = ast.FunctionDef | ast.AsyncFunctionDef


def _list_module_functions() -> dict[str, _FunctionDef]:
    """Lists the module-level functions of the agent's route module.

    Returns:
        Each function's definition, by name.
    """
    tree = ast.parse(inspect.getsource(command_routes))
    return {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }


def _extract_mounted_route(func: _FunctionDef) -> str | None:
    """Extracts the protocol constant a handler is mounted on.

    Args:
        func: A function of the route module.

    Returns:
        The constant's name, from `@router.post(f"/{NAME}")`, or None if the
        function is not a handler.
    """
    for decorator in func.decorator_list:
        match decorator:
            case ast.Call(
                func=ast.Attribute(attr="post"),
                args=[
                    ast.JoinedStr(values=[_, ast.FormattedValue(value=name)])
                ],
            ) if isinstance(name, ast.Name):
                return name.id
    return None


def _list_keys_read(
    func: _FunctionDef,
    functions: dict[str, _FunctionDef],
    seen: frozenset[str] = frozenset(),
) -> set[str]:
    """Lists the body keys a function reads, following the module's helpers.

    Args:
        func: The function to scan.
        functions: The module's functions, by name, to follow calls into.
        seen: Functions already on the call path, not scanned again.

    Returns:
        The string keys read.
    """
    keys: set[str] = set()
    for node in ast.walk(func):
        match node:
            case ast.Call(
                func=ast.Attribute(attr="get"),
                args=[ast.Constant(value=str(key)), *_],
            ):
                keys.add(key)
            case ast.Subscript(slice=ast.Constant(value=str(key))):
                keys.add(key)
            case ast.Call(
                func=ast.Name(id="_extract_required_field"),
                args=[_, ast.Constant(value=str(key))],
            ):
                keys.add(key)
            case ast.Call(func=ast.Name(id=callee)) if (
                callee in functions and callee not in seen
            ):
                keys |= _list_keys_read(
                    functions[callee], functions, seen | {func.name}
                )
    return keys


def _list_handlers() -> dict[str, _FunctionDef]:
    """Lists the agent's route handlers.

    Returns:
        Each handler's definition, by the protocol constant it is mounted on.
    """
    handlers = {}
    for func in _list_module_functions().values():
        if name := _extract_mounted_route(func):
            handlers[name] = func
    return handlers


_ROUTES = {
    name: value
    for name, value in vars(protocol).items()
    if isinstance(value, protocol.Route)
}


def test_every_route_is_mounted_by_the_agent() -> None:
    assert set(_list_handlers()) == set(_ROUTES)


@pytest.mark.parametrize("name", sorted(_ROUTES))
def test_route_args_are_the_keys_its_handler_reads(name: str) -> None:
    args = _ROUTES[name].args
    declared = set(args.__required_keys__ | args.__optional_keys__)

    read = _list_keys_read(_list_handlers()[name], _list_module_functions())

    assert read == declared


def test_a_route_is_its_path() -> None:
    route = protocol.INSIGHTS_GET_ROUTE

    assert route == "insights/get"
    assert f"/{route}" == "/insights/get"
    assert route.args is protocol.GetInsightArgs
