# Copyright 2025 Google LLC
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

import os

from jinja2 import Environment, FileSystemLoader


def _render_template(template_name: str) -> str:
    """Renders a template from the prompt-template directory."""
    current_dir = os.path.dirname(os.path.abspath(__file__))
    template_dir = os.path.join(current_dir, "prompt-template")
    env = Environment(loader=FileSystemLoader(template_dir))
    return env.get_template(template_name).render()


def load_few_shot_examples() -> str:
    """Loads and renders the Google Trends few-shot examples template.

    Returns:
        str: The rendered template with populated values.
    """
    try:
        return _render_template("google_trends_few_shots.j2")

    except Exception as e:
        print(f"Error loading few-shot examples template: {e!s}")
        raise


def load_table_structure_prompt() -> str:
    """Loads and renders the Google Trends table structure and rules template.

    Returns:
        str: The rendered template content.
    """
    try:
        return _render_template("google_trends_table_structure.j2")

    except Exception as e:
        print(f"Error loading table structure template: {e!s}")
        raise


def load_agent_instructions():
    """Dynamically loads agent instructions and few-shot examples."""
    try:
        # Load prompts
        table_structure_prompt = load_table_structure_prompt()
        few_shot_examples = load_few_shot_examples()

        # Combine prompts to form the full instruction
        full_instruction = f"{table_structure_prompt}\n\n{few_shot_examples}"
        return full_instruction

    except Exception as e:
        print(f"FATAL: Could not load agent instructions: {e}")
        # Fallback to a basic instruction if dynamic loading fails
        return "You are an agent that can query Google Trends data."
