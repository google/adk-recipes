"""Measure horizon's per-turn fixed prompt prefix. One consistent method.

Run: uv run python scripts/measure_prefix.py
Schema bytes use decl.model_dump_json(exclude_none=True) everywhere.
"""

from __future__ import annotations

import asyncio

from google.adk.models import LlmRequest


async def main() -> None:
    from horizon.agent import _SKILL_TOOLSET, root_agent

    tools = await root_agent.canonical_tools()
    names = [t.name for t in tools]

    # Measure post-normalization: ADK's non-pydantic path keeps source
    # indentation, which normalize_tool_schemas_callback strips per request.
    from horizon.context.schema_normalization import (
        normalize_tool_descriptions,
    )

    tool_req = LlmRequest()
    tool_req.append_tools(list(tools))
    normalize_tool_descriptions(tool_req)

    static = 0
    per_tool: list[tuple[int, str]] = []
    for entry in tool_req.config.tools or []:
        for decl in entry.function_declarations or []:
            size = len(decl.model_dump_json(exclude_none=True))
            static += size
            per_tool.append((size, decl.name or "?"))

    req = LlmRequest()
    await _SKILL_TOOLSET.process_llm_request(tool_context=None, llm_request=req)
    skills_block = req.config.system_instruction or ""
    idx = skills_block.find("<available_skills>")
    preamble = idx if idx >= 0 else len(skills_block)
    index = len(skills_block) - preamble if idx >= 0 else 0

    try:
        from horizon.subagents.descriptions import _build_suffix

        suffix = len(_build_suffix())
    except Exception:
        suffix = 0
    subagent_tools = [
        n for n in names if n in {"delegate", "agent", "subagent"}
    ]
    dynamic = suffix * len(subagent_tools)

    # Read the assembled static_instruction directly off the built agent
    # rather than reassembling it, so this script can never disagree with
    # what the app actually serves.
    static_instruction = root_agent.static_instruction or ""

    rows = [
        (
            "static instruction (root_agent.static_instruction)",
            len(static_instruction),
        ),
        ("skills preamble", preamble),
        ("<available_skills> index", index),
        (f"tool schemas static ({len(per_tool)} decls)", static),
        (f"dynamic desc suffix ({len(subagent_tools)} tools)", dynamic),
    ]
    total = sum(v for _, v in rows)
    width = max(len(k) for k, _ in rows)
    for key, value in rows:
        print(f"{key:<{width}}  {value:>7,}")
    print(
        f"{'TOTAL FIXED PREFIX':<{width}}  {total:>7,}  (~{total // 4:,} tok)"
    )
    print()
    print("largest tool schemas:")
    for size, name in sorted(per_tool, reverse=True)[:10]:
        print(f"  {size:>6,}  {name}")


if __name__ == "__main__":
    asyncio.run(main())
