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

"""Blog Skills Agent — Demonstrates 4 ways to use ADK Skills.

This agent showcases:
  1. Inline skills (models.Skill) — SEO checklist defined in code
  2. File-based skills (load_skill_from_dir) — Blog writer loaded from directory
  3. External skills — Content research skill loaded from a downloaded repo
  4. Meta skills — A skill-creator that generates new SKILL.md definitions
"""

import os
import pathlib

from google.adk import Agent
from google.adk.apps import App
from google.adk.skills import load_skill_from_dir, models
from google.adk.tools.skill_toolset import SkillToolset

# ---------------------------------------------------------------------------
# Pattern 1: Inline Skill — defined directly in Python code
# Best for: simple, stable rules that don't need external files
# ---------------------------------------------------------------------------
seo_skill = models.Skill(
    frontmatter=models.Frontmatter(
        name="seo-checklist",
        description=(
            "SEO optimization checklist for blog posts. Covers title tags,"
            " meta descriptions, heading structure, keyword placement,"
            " and readability best practices."
        ),
    ),
    instructions=(
        "When optimizing a blog post for SEO, check each item:\n\n"
        "1. **Title**: 50-60 chars, primary keyword near the start\n"
        "2. **Meta description**: 150-160 chars, includes a call-to-action\n"
        "3. **Headings**: H2/H3 hierarchy, keywords in 2-3 headings\n"
        "4. **First paragraph**: Primary keyword in first 100 words\n"
        "5. **Keyword density**: 1-2%, never forced or awkward\n"
        "6. **Paragraphs**: 2-3 sentences max, use bullet lists often\n"
        "7. **Links**: 2-3 internal + 3-5 external to authoritative sources\n"
        "8. **Images**: Alt text with keywords, compressed, descriptive names\n"
        "9. **URL slug**: Short, keyword-rich, hyphenated\n\n"
        "Review the content against each item and suggest specific improvements."
    ),
)


# ---------------------------------------------------------------------------
# Pattern 2: File-Based Skill — loaded from a local directory
# Best for: complex skills with reference docs, templates, or scripts
# The directory must contain SKILL.md and its name must match the skill name
# ---------------------------------------------------------------------------
blog_writer_skill = load_skill_from_dir(
    pathlib.Path(__file__).parent / "skills" / "blog-writer"
)


# ---------------------------------------------------------------------------
# Pattern 3: External Skill — loaded from a downloaded/cloned repository
# Best for: community skills, shared org standards, third-party capabilities
# Same as file-based, but the source is an external repo
# ---------------------------------------------------------------------------
content_researcher_skill = load_skill_from_dir(
    pathlib.Path(__file__).parent / "skills" / "content-research-writer"
)


# ---------------------------------------------------------------------------
# Pattern 4: Meta Skill — a skill that creates new skills
# Best for: self-extending agents that generate new capabilities on demand
# Inspired by the obra/superpowers writing-skills pattern
# ---------------------------------------------------------------------------
skill_creator = models.Skill(
    frontmatter=models.Frontmatter(
        name="skill-creator",
        description=(
            "Creates new ADK-compatible skill definitions from requirements."
            " Generates complete SKILL.md files following the Agent Skills"
            " specification at agentskills.io."
        ),
    ),
    instructions=(
        "When asked to create a new skill, generate a complete SKILL.md file.\n\n"
        "Read `references/skill-spec.md` for the format specification.\n"
        "Read `references/example-skill.md` for a working example.\n\n"
        "Follow these rules:\n"
        "1. Name must be kebab-case, max 64 characters\n"
        "2. Description must be under 1024 characters\n"
        "3. Instructions should be clear, step-by-step\n"
        "4. Reference files in references/ for detailed domain knowledge\n"
        "5. Keep SKILL.md under 500 lines — put details in references/\n"
        "6. Output the complete file content the user can save directly\n"
    ),
    resources=models.Resources(
        references={
            "skill-spec.md": (
                "# Agent Skills Specification (agentskills.io)\n\n"
                "## SKILL.md Format\n"
                "Every skill directory must contain a SKILL.md file.\n\n"
                "### Frontmatter (YAML)\n"
                "```yaml\n"
                "---\n"
                "name: my-skill-name          # kebab-case, max 64 chars\n"
                "description: What this skill does.  # max 1024 chars\n"
                "---\n"
                "```\n\n"
                "### Body (Markdown)\n"
                "The body contains the skill instructions. Write clear,\n"
                "step-by-step instructions the agent will follow.\n\n"
                "### Directory Structure\n"
                "```\n"
                "my-skill-name/\n"
                "  SKILL.md           # Required: metadata + instructions\n"
                "  references/        # Optional: detailed reference docs\n"
                "  assets/            # Optional: templates, data files\n"
                "  scripts/           # Optional: executable scripts\n"
                "```\n\n"
                "### Key Rules\n"
                "- Directory name MUST match the `name` field in frontmatter\n"
                "- Name must be kebab-case: ^[a-z0-9]+(-[a-z0-9]+)*$\n"
                "- Description is what the LLM uses to decide when to load the skill\n"
                "- Keep instructions actionable — tell the agent WHAT to do\n"
                "- Use `load_skill_resource` references for detailed docs\n"
            ),
            "example-skill.md": (
                "# Example: Code Review Skill\n\n"
                "```markdown\n"
                "---\n"
                "name: code-review\n"
                "description: Reviews Python code for correctness, style, "
                "and performance. Checks for common bugs, PEP 8 compliance, "
                "and suggests optimizations.\n"
                "---\n\n"
                "# Code Review Instructions\n\n"
                "When asked to review code:\n\n"
                "## Step 1: Read the Guidelines\n"
                "Use `load_skill_resource` to read `references/review-checklist.md`.\n\n"
                "## Step 2: Analyze\n"
                "Check the code against each item in the checklist.\n\n"
                "## Step 3: Report\n"
                "Provide findings organized by severity:\n"
                "- **Critical**: Bugs, security issues\n"
                "- **Warning**: Style violations, performance concerns\n"
                "- **Info**: Suggestions for improvement\n"
                "```\n"
            ),
        }
    ),
)


# ---------------------------------------------------------------------------
# Assemble: Package all skills into a single SkillToolset
# The toolset auto-registers list_skills, load_skill, and load_skill_resource
# ---------------------------------------------------------------------------
skill_toolset = SkillToolset(
    skills=[
        seo_skill,
        blog_writer_skill,
        content_researcher_skill,
        skill_creator,
    ]
)

root_agent = Agent(
    model=os.getenv("MODEL_NAME"),
    name="blog_skills_agent",
    description="A blog-writing agent powered by reusable skills.",
    instruction=(
        "You are a blog-writing assistant with specialized skills.\n\n"
        "You have four skills available:\n"
        "- **seo-checklist**: SEO optimization rules (load for SEO review)\n"
        "- **blog-writer**: Writing structure and style guide (load for writing)\n"
        "- **content-research-writer**: Research methodology (load for research)\n"
        "- **skill-creator**: Generate new skill definitions (load to create skills)\n\n"
        "When the user asks you to write, research, or optimize a blog post:\n"
        "1. Load the relevant skill(s) to get detailed instructions\n"
        "2. Use `load_skill_resource` to access reference materials\n"
        "3. Follow the skill's step-by-step instructions\n"
        "4. Apply multiple skills together when appropriate\n\n"
        "When the user asks you to create a new skill:\n"
        "1. Load the skill-creator skill\n"
        "2. Read the specification and example references\n"
        "3. Generate a complete SKILL.md that follows the spec\n\n"
        "Always explain which skill you're using and why."
    ),
    tools=[skill_toolset],
)

app = App(root_agent=root_agent, name="app")
