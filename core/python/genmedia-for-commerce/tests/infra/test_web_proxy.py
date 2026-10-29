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

"""``infra/web_proxy.sh`` (make web, and the end of make deploy and make
register-gemini-enterprise) with a fake gcloud, and the config.env values
that reach the shell and Cloud Run with their backslashes."""

import re
import socket
import subprocess
import sys

import pytest
import yaml
from dotenv import dotenv_values

from tests.infra.web_proxy_fakes import (
    IMPERSONATE,
    ROOT,
    free_port,
    make_fake,
    run,
    with_component,
)

MAKEFILE = (ROOT / "Makefile").read_text(encoding="utf-8")
SCRIPT_TEXT = (ROOT / "infra" / "web_proxy.sh").read_text(encoding="utf-8")
CLOUD_SHELL_ORIGIN = r"regex:^https://8080-[a-z0-9.-]+\.cloudshell\.dev$"
ACCESS = (
    "Only people with access to the project, or added with make share, "
    "can open it."
)


@pytest.fixture
def fake(tmp_path):
    return make_fake(tmp_path)


def test_the_hint_names_make_web_and_the_gcloud_command(fake):
    result, calls = run(fake, "--hint", WEB_PROXY_PORT="8080")

    assert result.returncode == 0
    assert result.stdout.splitlines() == [
        "Open the web app: make web (or: gcloud run services proxy my-svc "
        "--region europe-west1 --project my-proj --port 8080)",
        "then http://localhost:8080 (in Cloud Shell: Web Preview > Preview "
        f"on port 8080). {ACCESS}",
        "Right after a deploy, a 403 means the access grant is still "
        "applying: wait a minute and reload.",
    ]
    assert not calls


def test_the_background_start_needs_a_terminal(fake):
    """CI, an agent or a pipe: the hint instead of a proxy."""
    with_component(fake)

    result, calls = run(fake, "--background")

    assert result.returncode == 0
    assert result.stdout.startswith("Open the web app: make web")
    assert not calls


def test_make_web_runs_the_proxy_in_the_foreground(fake):
    """With a token of genmedia-web for the service URL, the proxy's target."""
    with_component(fake)
    port = str(free_port())

    result, calls = run(fake, WEB_PROXY_PORT=port)

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        f"Web app: http://localhost:{port} (in Cloud Shell: Web Preview > "
        f"Preview on port {port}). Ctrl+C stops it. {ACCESS}",
        "proxying",
    ]
    assert calls[-3:] == [
        "run services describe my-svc --region europe-west1 --project "
        "my-proj --format=value(status.url)",
        IMPERSONATE,
        "run services proxy my-svc --region europe-west1 --project my-proj "
        f"--port {port} --quiet --token=sa-token-1",
    ]


def test_a_missing_component_is_installed_first(fake):
    result, calls = run(fake)

    assert result.returncode == 0, result.stderr
    assert "components install cloud-run-proxy --quiet" in calls
    assert calls[-1].startswith("run services proxy my-svc")


def test_a_busy_port_is_an_error(fake):
    with_component(fake)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen()
        port = str(sock.getsockname()[1])

        result, calls = run(fake, WEB_PROXY_PORT=port)

    assert result.returncode == 1
    assert f"port {port} is in use" in result.stderr
    assert not calls


def test_no_project_is_an_error(fake):
    result, calls = run(fake, PROJECT_ID="")

    assert result.returncode == 1
    assert "PROJECT_ID is not set" in result.stderr
    assert not calls


# ------------------------------------------------------------- Makefile


def _recipe(target: str) -> str:
    return MAKEFILE.split(f"\n{target}:", 1)[1].split("\n\n", 1)[0]


def test_make_web_reads_the_service_from_config_env():
    assert _recipe("web").endswith('@eval "$$($(CONFIG_SH))" && $(WEB_PROXY)')
    assert "bash infra/web_proxy.sh" in MAKEFILE


def test_make_deploy_starts_the_proxy_after_a_successful_deploy():
    recipe = _recipe("deploy")

    assert (
        "= 0 ] && { echo; $(GRANT_WEB_INVOKER); "
        "$(WEB_PROXY) --background || true; exit 0; }" in recipe
    )
    assert "register-gemini-enterprise" not in recipe


def test_the_script_mints_the_token_as_genmedia_web_then_falls_back():
    sa_token = SCRIPT_TEXT.index("token=$(web_sa_token")
    own_token = SCRIPT_TEXT.index("token=$(gcloud auth print-identity-token")

    assert sa_token < own_token
    assert '--impersonate-service-account="$WEB_SA"' in SCRIPT_TEXT
    assert '--audiences="$url"' in SCRIPT_TEXT
    assert "--format='value(status.url)'" in SCRIPT_TEXT
    assert '"${PROXY[@]}" --token="$1"' in SCRIPT_TEXT
    assert 'exec "${PROXY[@]}"' not in SCRIPT_TEXT


def test_the_script_refreshes_the_token_in_its_watch_loop():
    serve = SCRIPT_TEXT.split("\nserve() {", 1)[1].split("\n}\n", 1)[0]

    assert 'REFRESH="${WEB_PROXY_REFRESH:-3000}"' in SCRIPT_TEXT  # 50 min
    assert 'while kill -0 "$proxy"' in serve
    assert 'session_ended "$session"' in serve
    assert '[ "$SECONDS" -lt "$next" ] || refresh_proxy' in serve
    assert re.search(r"trap '.*kill_tree \"\$proxy\".*' TERM HUP INT", serve)
    assert 'serve "$1"' in SCRIPT_TEXT  # the background watcher
    assert 'serve ""' in SCRIPT_TEXT  # make web


def test_registration_starts_the_proxy_whatever_its_outcome():
    recipe = _recipe("register-gemini-enterprise")

    # After a success or a failure, not in a dry run; the script's status
    assert "--dry-run); status=$$?; \\" in recipe
    assert 'if [ -z "$(DRY_RUN)" ]' in recipe
    assert "$(WEB_PROXY) --background || true" in recipe
    assert recipe.endswith("exit $$status")


# ------------------------------------------- config.env to shell and YAML


def _makefile_python(name: str) -> str:
    match = re.search(rf'^{name} = uv run python -c "(.*)"$', MAKEFILE, re.M)
    assert match, name
    return match.group(1)


@pytest.fixture
def example_config(tmp_path):
    (tmp_path / "config.env").write_text(
        (ROOT / "config.env.example").read_text()
    )
    return tmp_path


def test_the_example_origins_reach_the_shell_whole(example_config):
    """CONFIG_SH: what make deploy and make web eval."""
    lines = subprocess.run(
        [sys.executable, "-c", _makefile_python("CONFIG_SH")],
        cwd=example_config,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    origins = subprocess.run(
        ["bash", "-c", 'eval "$1" && printf %s "$ALLOW_ORIGINS"', "_", lines],
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    assert origins.endswith(f",{CLOUD_SHELL_ORIGIN}")
    assert (
        origins == dotenv_values(ROOT / "config.env.example")["ALLOW_ORIGINS"]
    )


def test_the_example_origins_reach_cloud_run_whole(example_config):
    """CLOUDRUN_ENV_YAML: make deploy's --env-vars-file."""
    env_yaml = subprocess.run(
        [sys.executable, "-c", _makefile_python("CLOUDRUN_ENV_YAML")],
        cwd=example_config,
        env={"COMMIT_SHA": "abc"},
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    deployed = yaml.safe_load(env_yaml)["ALLOW_ORIGINS"]
    assert deployed.split(",")[-1] == CLOUD_SHELL_ORIGIN
