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

"""Builds a persona's git home snapshot and thread journal from `tests/eval/fixtures/<persona>/`."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any

import yaml

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
CONTAINER_HOME = "/home/agent"
_DAYS_AGO = re.compile(r"@DAYS_AGO_(\d+)@")
_ALIAS = re.compile(r"\{([a-z_]+)\}")


@dataclass
class Persona:
    name: str
    root: Path
    data: dict[str, Any]
    snapshot: dict[str, Any]
    today: date = field(default_factory=date.today)
    ids: dict[str, str] = field(default_factory=dict)

    @property
    def journal(self) -> dict[str, dict[str, Any]]:
        return self.data.get("journal") or {}

    def render(self, value: Any) -> Any:
        if isinstance(value, str):
            text = _DAYS_AGO.sub(
                lambda m: self.day(int(m.group(1))).isoformat(), value
            )
            text = text.replace("@TODAY@", self.today.isoformat())
            return _ALIAS.sub(
                lambda m: self.ids.get(m.group(1), m.group(0)), text
            )
        if isinstance(value, list):
            return [self.render(v) for v in value]
        if isinstance(value, dict):
            return {k: self.render(v) for k, v in value.items()}
        return value

    def day(self, days_ago: int) -> date:
        return self.today - timedelta(days=days_ago)

    def projects(self) -> dict[str, str]:
        return {
            alias: self.render(spec["project"])
            for alias, spec in self.journal.items()
        }


def load_persona(
    name: str = "alex",
    *,
    today: date | None = None,
    ids: dict[str, str] | None = None,
) -> Persona:
    root = FIXTURES / name
    data = yaml.safe_load((root / "persona.yaml").read_text(encoding="utf-8"))
    snapshot = yaml.safe_load(
        (root / "snapshot.yaml").read_text(encoding="utf-8")
    )
    journal = data.get("journal") or {}
    return Persona(
        name=name,
        root=root,
        data=data,
        snapshot=snapshot,
        today=today or date.today(),
        ids=ids
        or {alias: f"thread-{uuid.uuid4().hex[:8]}" for alias in journal},
    )


def included_journal(
    persona: Persona, *, omit: Iterable[str] = (), extra: Iterable[str] = ()
) -> list[str]:
    skipped = set(omit)
    linked = [
        r["journal"]
        for r in persona.snapshot.get("repos") or []
        if r.get("journal")
    ]
    return [a for a in dict.fromkeys([*linked, *extra]) if a not in skipped]


def _git(cwd: Path, *args: str, when: datetime | None = None) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "ADK Voice Agent",
        "GIT_AUTHOR_EMAIL": "agent@voice-agent.local",
        "GIT_COMMITTER_NAME": "ADK Voice Agent",
        "GIT_COMMITTER_EMAIL": "agent@voice-agent.local",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
    }
    if when is not None:
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when.isoformat()
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} in {cwd} failed: {result.stderr.strip()[:400]}"
        )
    return result.stdout


def _copy_tree(persona: Persona, src: Path, dest: Path) -> None:
    for path in sorted(src.rglob("*")):
        if "__pycache__" in path.parts or path.suffix == ".pyc":
            continue
        target = dest / path.relative_to(src)
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        text = path.read_text(encoding="utf-8")
        target.write_text(persona.render(text), encoding="utf-8")


def build_home(
    persona: Persona,
    dest: Path,
    *,
    omit: Iterable[str] = (),
    extra: Iterable[str] = (),
) -> list[str]:
    dest = dest.resolve()
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    journal = included_journal(persona, omit=omit, extra=extra)
    projects = persona.projects()
    made = []
    for spec in persona.snapshot.get("repos") or []:
        if spec.get("journal") and spec["journal"] not in journal:
            continue
        rel = persona.render(spec["path"])
        repo = dest / rel
        repo.mkdir(parents=True)
        _git(repo, "init", "--quiet", "-b", "main")
        for commit in spec["commits"]:
            when = datetime.combine(
                persona.day(int(commit.get("days_ago", 0))), time(16, 10)
            ).astimezone()
            branch = commit.get("branch")
            if branch:
                _git(repo, "switch", "--quiet", "-c", branch)
            _copy_tree(persona, persona.root / commit["tree"], repo)
            _git(repo, "add", "-A")
            _git(
                repo,
                "commit",
                "--quiet",
                "-m",
                persona.render(commit["message"]),
                when=when,
            )
            if branch:
                _git(repo, "switch", "--quiet", "main")
                merge = persona.render(commit.get("merge") or f"Merge {branch}")
                _git(
                    repo,
                    "merge",
                    "--quiet",
                    "--no-ff",
                    "-m",
                    merge,
                    branch,
                    when=when,
                )
        if spec.get("thread_branches"):
            for alias in journal:
                if projects.get(alias) == rel:
                    _git(repo, "branch", f"agent/{persona.ids[alias]}", "main")
        if origin := spec.get("origin"):
            bare = dest / origin
            bare.parent.mkdir(parents=True, exist_ok=True)
            _git(dest, "init", "--quiet", "--bare", "-b", "main", str(bare))
            _git(repo, "remote", "add", "origin", str(bare))
            _git(repo, "push", "--quiet", "-u", "origin", "--all")
            # The checkout lives at the same place inside the computer.
            _git(
                repo,
                "remote",
                "set-url",
                "origin",
                f"{CONTAINER_HOME}/{origin}",
            )
        made.append(rel)
    return made
