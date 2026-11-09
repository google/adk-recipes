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

"""
Google ADK & GenAI Compatibility Bridge for the Travel Concierge Example.

Uses native `google-adk` and `google-genai` classes when installed (e.g. in
`adk web examples/`), and falls back to lightweight stdlib shims so offline CI
tests (`pytest` / `unittest`) run without requiring cloud SDKs.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any


def normalize_adk_model_id(raw_model: str) -> str:
    """Strips `publishers/google/models/` so ADK's `LLMRegistry` resolves native `Gemini`."""
    cleaned = str(raw_model or "").strip()
    if cleaned.lower().startswith("publishers/google/models/"):
        return cleaned[len("publishers/google/models/") :]
    return cleaned


def _load_env_example_defaults() -> dict[str, str]:
    """Reads baseline values declared in the recipe `.env.example`."""
    env_example = Path(__file__).resolve().parents[2] / ".env.example"
    defaults: dict[str, str] = {}
    if not env_example.is_file():
        return defaults
    for raw_line in env_example.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        defaults[key.strip()] = val.strip().strip("'\"")
    return defaults


_ENV_EXAMPLE_DEFAULTS = _load_env_example_defaults()
_raw_flash_model = os.environ.get("TOKENOMICS_DEFAULT_MODEL")
_raw_frontier_model = os.environ.get("TOKENOMICS_FRONTIER_MODEL")

FLASH_MODEL: str = normalize_adk_model_id(
    _raw_flash_model
    if _raw_flash_model
    else _ENV_EXAMPLE_DEFAULTS.get(
        "TOKENOMICS_DEFAULT_MODEL", "gemini-3.8-flash"
    )
)
FRONTIER_MODEL: str = normalize_adk_model_id(
    _raw_frontier_model
    if _raw_frontier_model
    else _ENV_EXAMPLE_DEFAULTS.get(
        "TOKENOMICS_FRONTIER_MODEL", "gemini-4-argon"
    )
)

try:
    from google.adk.agents import LlmAgent
    from google.adk.apps import App
    from google.adk.apps.app import ContextCacheConfig, EventsCompactionConfig
    from google.adk.planners import BuiltInPlanner
    from google.adk.skills import load_skill_from_dir
    from google.adk.tools import FunctionTool
    from google.adk.tools.agent_tool import AgentTool
    from google.adk.tools.skill_toolset import SkillToolset
    from google.genai import types as genai_types

    HAS_GOOGLE_ADK = True
except Exception:
    HAS_GOOGLE_ADK = False

    class _ThinkingLevel(StrEnum):
        MINIMAL = "MINIMAL"
        LOW = "LOW"
        MEDIUM = "MEDIUM"
        HIGH = "HIGH"

    @dataclass
    class _ThinkingConfig:
        include_thoughts: bool = False
        thinking_level: str | None = None
        thinking_budget: int | None = None

    @dataclass
    class _GenerateContentConfig:
        temperature: float | None = None
        thinking_config: _ThinkingConfig | None = None

    class _GenAITypesNamespace:
        ThinkingLevel = _ThinkingLevel
        ThinkingConfig = _ThinkingConfig
        GenerateContentConfig = _GenerateContentConfig

    genai_types = _GenAITypesNamespace()  # type: ignore[assignment]

    @dataclass
    class BuiltInPlanner:  # type: ignore[no-redef]
        thinking_config: Any

    @dataclass
    class ContextCacheConfig:  # type: ignore[no-redef]
        min_tokens: int = 1024
        ttl_seconds: int = 300
        cache_intervals: int = 10

    @dataclass
    class EventsCompactionConfig:  # type: ignore[no-redef]
        compaction_interval: int = 4
        overlap_size: int = 1

    class FunctionTool:  # type: ignore[no-redef]
        def __init__(self, func: Callable[..., Any]):
            self.func = func
            self.name = getattr(func, "__name__", "tool")

        def __call__(self, *args: Any, **kwargs: Any) -> Any:
            return self.func(*args, **kwargs)

    class AgentTool:  # type: ignore[no-redef]
        def __init__(self, agent: Any, include_plugins: bool = True):
            self.agent = agent
            self.name = getattr(agent, "name", "agent_tool")
            self.include_plugins = include_plugins

    @dataclass
    class _ShimSkill:
        name: str
        description: str
        instructions: str

    def load_skill_from_dir(skill_dir: str | Path) -> Any:
        p = Path(skill_dir) / "SKILL.md"
        text = p.read_text(encoding="utf-8") if p.is_file() else ""
        return _ShimSkill(
            name=Path(skill_dir).name,
            description=f"Travel guide for {Path(skill_dir).name}",
            instructions=text,
        )

    class SkillToolset:  # type: ignore[no-redef]
        def __init__(self, skills: list[Any]):
            self.skills = list(skills)
            self.name = "skill_toolset"

    @dataclass
    class LlmAgent:  # type: ignore[no-redef]
        name: str
        model: str = FLASH_MODEL
        description: str = ""
        instruction: str = ""
        tools: list[Any] = field(default_factory=list)
        sub_agents: list[Any] = field(default_factory=list)
        planner: Any | None = None
        generate_content_config: Any | None = None
        parent_agent: Any | None = None

        def __post_init__(self) -> None:
            for sub in self.sub_agents:
                sub.parent_agent = self

    @dataclass
    class App:  # type: ignore[no-redef]
        name: str
        root_agent: Any
        plugins: list[Any] = field(default_factory=list)
        context_cache_config: Any | None = None
        events_compaction_config: Any | None = None


def build_planner(
    model_name: str,
    tier: str,
) -> tuple[Any, Any]:
    """Builds `(BuiltInPlanner, GenerateContentConfig)` compatible with Gemini 2.5, 3.x, and 4.x."""
    tier_low = tier.strip().lower()
    clean = model_name.rsplit("/", maxsplit=1)[-1].strip().lower()

    if clean.startswith(("gemini-3", "gemini-4")):
        # Vertex AI Gemini 3.8 Flash and Gemini 4 Argon support LOW / MEDIUM / HIGH
        lvl_map = {
            "minimal": genai_types.ThinkingLevel.LOW,
            "low": genai_types.ThinkingLevel.LOW,
            "medium": genai_types.ThinkingLevel.MEDIUM,
            "high": genai_types.ThinkingLevel.HIGH,
        }
        lvl = lvl_map[tier_low]
        return (
            BuiltInPlanner(
                thinking_config=genai_types.ThinkingConfig(
                    include_thoughts=(tier_low != "minimal"),
                    thinking_level=lvl,
                )
            ),
            genai_types.GenerateContentConfig(temperature=1.0),
        )

    if clean.startswith("gemini-2.5"):
        budgets = {"minimal": 0, "low": 256, "medium": 1024, "high": 4096}
        b_val = budgets[tier_low]
        return (
            BuiltInPlanner(
                thinking_config=genai_types.ThinkingConfig(
                    include_thoughts=(b_val > 0),
                    thinking_budget=b_val,
                )
            ),
            genai_types.GenerateContentConfig(temperature=0.7),
        )

    return None, None


def load_travel_skill_toolset() -> Any:
    """Loads on-demand regional travel guides (`japan-guide`, `europe-guide`)."""
    skills_root = Path(__file__).resolve().parent / "skills"
    skills = [
        load_skill_from_dir(skills_root / slug)
        for slug in ("japan-guide", "europe-guide")
        if (skills_root / slug / "SKILL.md").is_file()
    ]
    return SkillToolset(skills=skills)
