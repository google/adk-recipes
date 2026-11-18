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

"""The computer image (`sandbox/`): pinned installs, and an idempotent first boot."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from app.workers.environment import COMPUTER_HOME, SANDBOX_IMAGE_VENV
from app.workers.providers.common import sandbox_owner
from scripts import computer_ssh

SANDBOX = Path(__file__).resolve().parents[2] / "sandbox"
DOCKERFILE = (SANDBOX / "Dockerfile").read_text()
ENTRYPOINT = (SANDBOX / "entrypoint.sh").read_text()


def _arg(name: str) -> str:
    match = re.search(rf"^ARG {name}=(\S+)$", DOCKERFILE, re.MULTILINE)
    assert match, f"Dockerfile pins no {name}"
    return match.group(1)


def test_every_install_is_pinned_and_verified() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", _arg("CLAUDE_CODE_VERSION"))
    assert re.fullmatch(r"\d+\.\d+\.\d+", _arg("MCP_REMOTE_VERSION"))
    assert re.fullmatch(r"\d+\.\d+\.\d+-\d+", _arg("AGY_RELEASE"))
    assert re.fullmatch(r"[0-9a-f]{128}", _arg("AGY_SHA512_AMD64"))
    assert re.fullmatch(r"[0-9a-f]{128}", _arg("AGY_SHA512_ARM64"))
    assert re.fullmatch(r"[0-9a-f]{64}", _arg("GH_SHA256_AMD64"))
    assert re.fullmatch(r"[0-9a-f]{64}", _arg("GH_SHA256_ARM64"))
    assert _arg("NODE_MAJOR") == "22"
    assert re.fullmatch(r"22\.\d+\.\d+", _arg("NODE_VERSION"))
    assert re.fullmatch(r"[0-9a-f]{64}", _arg("NODE_SHA256_AMD64"))
    assert re.fullmatch(r"[0-9a-f]{64}", _arg("NODE_SHA256_ARM64"))
    assert "@latest" not in DOCKERFILE and "releases/latest" not in DOCKERFILE
    # Downloads are checked before they run, never piped straight into a shell.
    assert "sha256sum -c" in DOCKERFILE and "sha512sum -c" in DOCKERFILE
    assert "install.sh | bash" not in DOCKERFILE
    assert "| bash" not in DOCKERFILE
    assert ">=" not in DOCKERFILE
    # agy doesn't replace the pinned version with a newer one at run time.
    assert "AGY_CLI_DISABLE_AUTO_UPDATE=1" in DOCKERFILE


def test_the_image_matches_the_layout_system_1_expects() -> None:
    assert f"HOME={COMPUTER_HOME}" in DOCKERFILE
    assert f"SANDBOX_WORKSPACE={COMPUTER_HOME}" in DOCKERFILE
    assert f"uv venv {SANDBOX_IMAGE_VENV}" in DOCKERFILE
    assert (
        f"PATH={COMPUTER_HOME}/.local/bin:{SANDBOX_IMAGE_VENV}/bin:"
        in DOCKERFILE
    )
    assert "--uid 1000" in DOCKERFILE
    assert (
        'ENTRYPOINT ["tini", "--", "/opt/computer/entrypoint.sh"]' in DOCKERFILE
    )
    # Nothing is installed into the home, which is the user's volume.
    for line in DOCKERFILE.splitlines():
        if line.startswith(("RUN", "COPY")):
            assert f" {COMPUTER_HOME}/" not in line, line
    assert (SANDBOX / "home" / "CLAUDE.md").read_text().strip() == "@AGENTS.md"
    assert (SANDBOX / "home" / "AGENTS.md").is_file()


def test_entrypoint_is_valid_posix_sh() -> None:
    check = subprocess.run(
        ["/bin/sh", "-n", str(SANDBOX / "entrypoint.sh")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert check.returncode == 0, check.stderr
    assert os.access(SANDBOX / "entrypoint.sh", os.X_OK)


def _boot(home: Path, seed: Path, **env: str) -> None:
    """Run the entrypoint's home setup (its embedded Python) against `home`."""
    script = ENTRYPOINT.split("<<'EOF'\n", 1)[1].split("\nEOF\n", 1)[0]
    subprocess.run(
        [sys.executable, "-", str(seed)],
        input=script,
        text=True,
        check=True,
        env={"PATH": os.environ.get("PATH", ""), "HOME": str(home), **env},
    )


@pytest.fixture
def seed(tmp_path: Path) -> Path:
    seed = tmp_path / "opt-computer"
    (seed / "home").mkdir(parents=True)
    for item in (SANDBOX / "home").rglob("*"):
        target = seed / "home" / item.relative_to(SANDBOX / "home")
        if item.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(item.read_bytes())
    skill = seed / "skills" / "starter"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("starter v1")
    return seed


def test_first_boot_lays_out_the_home(tmp_path: Path, seed: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _boot(
        home, seed, GOOGLE_CLOUD_PROJECT="proj", GOOGLE_CLOUD_LOCATION="global"
    )

    assert (home / "CLAUDE.md").read_text().strip() == "@AGENTS.md"
    for folder in ("inbox", "projects", "code", "scratch"):
        assert (home / folder).is_dir()
    store = home / ".agents" / "skills"
    assert (store / "starter" / "SKILL.md").read_text() == "starter v1"
    for link in (home / ".claude" / "skills", home / ".gemini/config/skills"):
        assert link.is_symlink() and link.resolve() == store.resolve()

    settings = json.loads(
        (home / ".gemini/antigravity-cli/settings.json").read_text()
    )
    assert (
        "modelProvider" not in settings
    )  # agy signs in with ADC (AGY_ADC_AUTH)
    assert settings["gcp"] == {"project": "proj", "location": "global"}
    assert "command(*)" in settings["permissions"]["allow"]
    mcp = json.loads((home / ".gemini/config/mcp_config.json").read_text())
    gateway = mcp["mcpServers"]["voice_agent"]
    # One stable entry for every thread: each run's environment supplies the URL and
    # the token, which never lands in a file.
    assert gateway == {
        "command": "sh",
        "args": [
            "-c",
            'exec mcp-remote "$VOICE_AGENT_MCP_URL" --transport http-only '
            '--header "Authorization: Bearer $VOICE_AGENT_MCP_TOKEN"',
        ],
    }


def test_later_boots_keep_the_users_edits(tmp_path: Path, seed: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _boot(home, seed)
    (home / "AGENTS.md").write_text("mine")
    (home / ".agents/skills/starter/SKILL.md").write_text("edited")
    settings_path = home / ".gemini/antigravity-cli/settings.json"
    settings = json.loads(settings_path.read_text())
    settings["model"] = "my model"
    settings["modelProvider"] = "vertex"
    settings["permissions"]["allow"].append("command(ls)")
    settings_path.write_text(json.dumps(settings))
    mcp_path = home / ".gemini/config/mcp_config.json"
    mcp = json.loads(mcp_path.read_text())
    mcp["mcpServers"]["mine"] = {"command": "my-server"}
    mcp_path.write_text(json.dumps(mcp))
    (seed / "skills" / "starter" / "SKILL.md").write_text("starter v2")

    _boot(home, seed)
    _boot(home, seed)

    assert (home / "AGENTS.md").read_text() == "mine"
    assert (home / ".agents/skills/starter/SKILL.md").read_text() == "edited"
    settings = json.loads(settings_path.read_text())
    assert settings["model"] == "my model"
    assert settings["modelProvider"] == "vertex"
    assert settings["permissions"]["allow"] == ["command(*)", "command(ls)"]
    servers = json.loads(mcp_path.read_text())["mcpServers"]
    assert set(servers) == {"voice_agent", "mine"}


def _seeded(home: Path) -> dict[str, str]:
    return json.loads((home / ".voice-agent" / "seeded.json").read_text())


def test_a_new_image_updates_the_files_the_user_left_alone(
    tmp_path: Path, seed: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _boot(home, seed)
    shipped = (seed / "home" / "AGENTS.md").read_bytes()
    assert _seeded(home)["AGENTS.md"] == hashlib.sha256(shipped).hexdigest()
    assert ".agents/skills/starter/SKILL.md" in _seeded(home)

    # The next image ships new versions of both, and a new skill file.
    (seed / "home" / "AGENTS.md").write_text("guide v2")
    (seed / "skills" / "starter" / "SKILL.md").write_text("starter v2")
    (seed / "skills" / "starter" / "reference.md").write_text("ref")
    _boot(home, seed)

    assert (home / "AGENTS.md").read_text() == "guide v2"
    assert (
        home / ".agents/skills/starter/SKILL.md"
    ).read_text() == "starter v2"
    assert (home / ".agents/skills/starter/reference.md").read_text() == "ref"
    assert _seeded(home)["AGENTS.md"] == hashlib.sha256(b"guide v2").hexdigest()


def test_edited_and_deleted_files_stay_the_users(
    tmp_path: Path, seed: Path
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    _boot(home, seed)
    (home / "AGENTS.md").write_text("mine")
    (home / ".agents/skills/starter/SKILL.md").unlink()
    (home / "CLAUDE.md").unlink()
    (home / "CLAUDE.md").symlink_to(home / "AGENTS.md")

    (seed / "home" / "AGENTS.md").write_text("guide v2")
    (seed / "home" / "CLAUDE.md").write_text("@AGENTS.md v2")
    (seed / "skills" / "starter" / "SKILL.md").write_text("starter v2")
    _boot(home, seed)
    _boot(home, seed)

    assert (home / "AGENTS.md").read_text() == "mine"
    assert (home / "CLAUDE.md").is_symlink()
    assert not (home / ".agents/skills/starter/SKILL.md").exists()


def test_the_shipped_guide_and_skills_are_complete() -> None:
    guide = (SANDBOX / "home" / "AGENTS.md").read_text()
    assert "Placeholder" not in guide
    names = sorted(
        p.parent.name for p in (SANDBOX / "skills").glob("*/SKILL.md")
    )
    assert names == [
        "approvals",
        "coding-workflow",
        "connected-apps-mcp",
        "deliverables",
        "filesystem",
        "recommendations",
    ]
    for name in names:
        text = (SANDBOX / "skills" / name / "SKILL.md").read_text()
        front = text.split("---\n")[1]
        assert f"name: {name}\n" in front
        assert re.search(r"^description: .{80,}$", front, re.MULTILINE), name
        # The guide points at every skill.
        assert f"`{name}`" in guide


# ---- make ssh / make doctor (scripts/computer_ssh.py) -------------------------

ACCOUNT = "alex@example.com"
NAME = f"voice-staging-{sandbox_owner(ACCOUNT)}"
CONFIG = {"computer_zone": "z", "computer_deployment": "voice-staging"}


def _gcloud(
    monkeypatch: pytest.MonkeyPatch, listing: str, returncode: int = 0
) -> tuple[list[list[str]], list[list[str]]]:
    calls: list[list[str]] = []

    def run(
        argv: list[str], **_kwargs: object
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[1] == "config":
            return subprocess.CompletedProcess(
                argv, 0, ACCOUNT.upper() + "\n", ""
            )
        return subprocess.CompletedProcess(argv, returncode, listing, "denied")

    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setattr(computer_ssh, "deploy_config", lambda *_args: CONFIG)
    execs: list[list[str]] = []
    monkeypatch.setattr(
        computer_ssh.os, "execvp", lambda _file, argv: execs.append(argv)
    )
    return calls, execs


def test_running_computer_execs_ssh_with_the_remote_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls, execs = _gcloud(
        monkeypatch, f"{NAME}-old\tTERMINATED\n{NAME}\tRUNNING\n"
    )
    computer_ssh.main(["--project=p", "--", "-t", "sudo", "docker", "exec"])
    assert f"--filter=name={NAME}" in calls[1]
    assert execs == [
        [
            "gcloud",
            "compute",
            "ssh",
            NAME,
            "--project=p",
            "--zone=z",
            "--tunnel-through-iap",
            "--quiet",
            "--",
            "-t",
            "sudo",
            "docker",
            "exec",
        ]
    ]


@pytest.mark.parametrize(
    ("listing", "message"),
    [
        (
            f"{NAME}\tTERMINATED\n",
            "Your computer is stopped; open the app to start it.",
        ),
        (
            f"{NAME}\tSTOPPING\n",
            "Your computer is stopped; open the app to start it.",
        ),
        (
            f"{NAME}\tSTAGING\n",
            "Your computer is starting; try again in a minute.",
        ),
        ("", "You don't have a computer yet; open the app to create it."),
        (
            f"{NAME}\tREPAIRING\n",
            "Your computer is REPAIRING; open the app to check on it.",
        ),
    ],
)
def test_a_computer_that_isnt_running_gets_a_plain_answer(
    monkeypatch: pytest.MonkeyPatch, listing: str, message: str
) -> None:
    _calls, execs = _gcloud(monkeypatch, listing)
    with pytest.raises(SystemExit) as exit_info:
        computer_ssh.main(["--project=p", "--", "true"])
    assert exit_info.value.code == message
    assert execs == []


def test_a_failed_lookup_shows_gcloud_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _calls, execs = _gcloud(monkeypatch, "", returncode=1)
    with pytest.raises(SystemExit) as exit_info:
        computer_ssh.main(["--project=p"])
    assert exit_info.value.code == "❌ denied"
    assert execs == []
