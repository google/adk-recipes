#!/usr/bin/env python3
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

"""Validate that recipes with a root Dockerfile build and serve properly.

Given one or more recipe directories (or --all / stdin paths):
1. Verifies the recipe contains a Dockerfile at the recipe root.
2. Builds the Docker image for the recipe (platform linux/amd64).
3. Runs the container with a test environment (populated from .env.example).
4. Probes the running container to verify that the agent/service is accessible.
5. Emits clear diagnostics, error annotations, and actionable solutions on failure.
6. Cleans up all test containers and images.

Usage:
  python3 .github/scripts/check_recipe_docker.py core/python/ambient-expense-agent
  python3 .github/scripts/check_recipe_docker.py --all

Exit codes:
  0  all target recipe Dockerfiles built and verified accessible (or none in scope)
  1  one or more recipe containers failed to build or serve
  2  CI tooling failure
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "tools"))
from ci_message import (  # noqa: E402
    EXIT_OK,
    Diagnostic,
    Doc,
    guard,
    infra_fault,
    report,
    report_infra_fault,
)

CHECKER = "check_recipe_docker.py"
RECIPE_ROOTS = ("core", "contrib", "skills")
DEFAULT_DOCKER_TIMEOUT = 30

DEFAULT_PROBE_PATHS = (
    "/list-apps",
    "/docs",
    "/health",
    "/healthz",
    "/api/status",
    "/",
)

DEFAULT_CONTAINER_ENV = {
    "USE_IN_MEMORY_SESSION": "true",
    "INTEGRATION_TEST": "1",
    "GOOGLE_CLOUD_PROJECT": "adk-verify-placeholder",
    "GOOGLE_CLOUD_LOCATION": "global",
    "GOOGLE_GENAI_USE_VERTEXAI": "True",
    "MODEL_NAME": "gemini-3.5-flash",
}


@dataclass
class ValidationResult:
    """Result of Docker build and accessibility validation for a recipe."""

    recipe: str
    has_dockerfile: bool = False
    build_passed: bool = False
    run_passed: bool = False
    accessible_endpoint: str | None = None
    error_message: str | None = None
    solution: str | None = None
    log_tail: str | None = None
    details: dict[str, str] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.has_dockerfile and self.build_passed and self.run_passed


def parse_env_example(path: Path) -> dict[str, str]:
    """Parse a recipe's .env.example into key-value pairs."""
    if not path.is_file():
        return {}
    env: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if not key.isidentifier():
            continue
        val = value.split(" #")[0].strip().strip("'\"")
        if val.startswith("<TODO") and val.endswith(">"):
            continue
        env[key] = val
    return env


def sanitize_tag(name: str) -> str:
    """Create a valid Docker tag component from a recipe name/path."""
    clean = re.sub(r"[^a-zA-Z0-9_.-]+", "-", name).lower().strip("-")
    return clean or "recipe"


def run_cmd(
    cmd: list[str],
    *,
    timeout: int = 600,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a command, capturing stdout/stderr without raising."""
    try:
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            cwd=cwd,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=124,
            stdout="",
            stderr=f"Command '{' '.join(cmd)}' timed out after {timeout}s",
        )
    except FileNotFoundError as e:
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=127,
            stdout="",
            stderr=str(e),
        )


def parse_host_port(docker_port_output: str) -> int | None:
    """Extract host port from 'docker port <container> <port>' output."""
    lines = [
        ln.strip()
        for ln in (docker_port_output or "").strip().splitlines()
        if ln.strip()
    ]
    ipv4 = [ln for ln in lines if re.match(r"^\d+\.\d+\.\d+\.\d+:\d+$", ln)]
    for candidate in (*ipv4, *lines):
        match = re.search(r":(\d+)$", candidate)
        if match:
            try:
                return int(match.group(1))
            except ValueError:
                continue
    return None


def probe_http(url: str, timeout: int = 5) -> tuple[int | None, str]:
    """Send an HTTP GET request. Returns (status_code, body_or_error)."""
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "adk-docker-validator/1.0"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read(2048).decode("utf-8", "replace")
            return resp.status, body
    except urllib.error.HTTPError as e:
        return e.code, e.reason or ""
    except Exception as e:
        return None, str(e)


def diagnose_build_failure(stderr: str, stdout: str) -> tuple[str, str]:
    """Analyze Docker build failure output to provide clear cause and solution."""
    combined = (stderr + "\n" + stdout).lower()

    if "copy" in combined and (
        "not found" in combined
        or "no such file" in combined
        or "failed to calculate checksum" in combined
    ):
        return (
            "A file or directory referenced in a COPY instruction was not found in the build context.",
            "Ensure that all files copied by the Dockerfile exist in the recipe directory, "
            "or are created conditionally (e.g. 'COPY file.example* file* ./' or 'RUN mkdir -p assets').",
        )

    if (
        "uv sync" in combined or "uv.lock" in combined
    ) and "failed" in combined:
        return (
            "uv dependency synchronization failed inside the container during build.",
            "Check that pyproject.toml and uv.lock are synchronized by running 'uv lock' in the recipe directory, "
            "and ensure required dependencies have compatible wheels for the container's Python version.",
        )

    if ("npm" in combined or "yarn" in combined) and "failed" in combined:
        return (
            "Frontend build failed inside the container.",
            "Verify that frontend package.json dependencies install cleanly and 'npm run build' succeeds.",
        )

    if "returned a non-zero code" in combined:
        return (
            "A RUN instruction in the Dockerfile exited with an error.",
            "Review the build log tail above to identify the failing command and fix the Dockerfile or recipe scripts.",
        )

    return (
        "Docker image build failed.",
        "Check the build log tail above for compilation or configuration errors in the Dockerfile.",
    )


def diagnose_runtime_failure(logs: str) -> tuple[str, str]:
    """Analyze container runtime logs to identify startup crashes."""
    lowered = logs.lower()

    if "validationerror" in lowered or "pydantic" in lowered:
        return (
            "The application crashed on startup due to a Pydantic / configuration validation error.",
            "Ensure all required settings (such as MODEL_NAME) have defaults in code, fallback values, "
            "or are documented in .env.example.",
        )

    if "modulenotfounderror" in lowered or "importerror" in lowered:
        return (
            "The application failed to import a required module on startup.",
            "Ensure all imports are declared in pyproject.toml / requirements and installed in the Dockerfile.",
        )

    if (
        "defaultcredentialserror" in lowered
        or "could not automatically determine credentials" in lowered
    ):
        return (
            "The application attempted to access live Google Cloud credentials at module/import time.",
            "Ensure import-time GCP client initialization handles missing credentials gracefully (e.g. try/except "
            "or gating behind USE_IN_MEMORY_SESSION / INTEGRATION_TEST).",
        )

    if "address already in use" in lowered or "port" in lowered:
        return (
            "The application had a port binding or server startup issue.",
            "Verify that the container command binds to host 0.0.0.0 and the port configured in PORT (default 8080).",
        )

    return (
        "The container process terminated unexpectedly before serving.",
        "Inspect the container logs above for unhandled exceptions during application startup.",
    )


def validate_recipe_docker(
    recipe_dir: Path,
    *,
    build_timeout: int = 900,
    probe_timeout: int = 90,
    port: int = 8080,
    probe_paths: tuple[str, ...] = DEFAULT_PROBE_PATHS,
) -> ValidationResult:
    """Build Dockerfile and verify container accessibility for a recipe directory."""
    recipe_rel = os.path.relpath(recipe_dir, REPO_ROOT)
    dockerfile_path = recipe_dir / "Dockerfile"

    if not dockerfile_path.is_file():
        return ValidationResult(
            recipe=recipe_rel,
            has_dockerfile=False,
            error_message=f"No Dockerfile found at root of recipe: {recipe_rel}",
        )

    tag_suffix = sanitize_tag(recipe_rel)
    image_tag = f"adk-verify/{tag_suffix}:test"
    container_name = f"adk-verify-{tag_suffix}-{int(time.time())}"

    result = ValidationResult(recipe=recipe_rel, has_dockerfile=True)

    # 1. Build the Docker image
    print(f"\n[BUILD] Building Docker image for {recipe_rel} ({image_tag})...")
    build_proc = run_cmd(
        [
            "docker",
            "build",
            "--platform",
            "linux/amd64",
            "-t",
            image_tag,
            "-f",
            str(dockerfile_path),
            str(recipe_dir),
        ],
        timeout=build_timeout,
    )

    if build_proc.returncode != 0:
        result.build_passed = False
        tail_output = (build_proc.stderr or build_proc.stdout).strip()
        lines = tail_output.splitlines()
        result.log_tail = "\n".join(lines[-35:]) if lines else "No output"
        err_reason, solution = diagnose_build_failure(
            build_proc.stderr, build_proc.stdout
        )
        result.error_message = err_reason
        result.solution = solution

        # Cleanup image if partially tagged
        run_cmd(
            ["docker", "rmi", "-f", image_tag], timeout=DEFAULT_DOCKER_TIMEOUT
        )
        return result

    result.build_passed = True
    print(f"[PASS] Docker image built successfully: {image_tag}")

    # 2. Run the container and test accessibility
    try:
        # Assemble environment variables: .env.example first, default overrides second
        container_env = dict(DEFAULT_CONTAINER_ENV)
        env_example = parse_env_example(recipe_dir / ".env.example")
        # Apply .env.example values (except keys overridden by container_env)
        for k, v in env_example.items():
            if k not in container_env:
                container_env[k] = v

        container_env["PORT"] = str(port)

        env_args: list[str] = []
        for k, v in container_env.items():
            env_args.extend(["-e", f"{k}={v}"])

        # Ensure no leftover container with this name
        run_cmd(
            ["docker", "rm", "-f", container_name],
            timeout=DEFAULT_DOCKER_TIMEOUT,
        )

        print(
            f"[RUN] Starting test container {container_name} on port {port}..."
        )
        run_proc = run_cmd(
            [
                "docker",
                "run",
                "-d",
                "--name",
                container_name,
                "-p",
                f"127.0.0.1::{port}",
                *env_args,
                image_tag,
            ],
            timeout=60,
        )

        if run_proc.returncode != 0:
            result.run_passed = False
            result.error_message = (
                f"Failed to start Docker container: {run_proc.stderr.strip()}"
            )
            result.solution = "Ensure Docker daemon has sufficient resources and port mappings are available."
            result.log_tail = run_proc.stderr.strip()
            return result

        # Get the ephemeral host port assigned by Docker
        port_proc = run_cmd(
            ["docker", "port", container_name, str(port)],
            timeout=DEFAULT_DOCKER_TIMEOUT,
        )
        host_port = parse_host_port(port_proc.stdout)
        if host_port is None:
            result.run_passed = False
            result.error_message = (
                f"Port {port} was not published by container {container_name}."
            )
            result.solution = f"Ensure Dockerfile EXPOSEs port {port} and the entrypoint binds to 0.0.0.0:{port}."
            result.log_tail = _get_container_logs(container_name)
            return result

        base_url = f"http://127.0.0.1:{host_port}"
        print(
            f"[PROBE] Probing service at {base_url} (timeout: {probe_timeout}s)..."
        )

        # 3. Polling loop to test accessibility
        deadline = time.monotonic() + probe_timeout
        serving_endpoint: str | None = None

        while time.monotonic() < deadline:
            # Check if container is still running
            inspect_proc = run_cmd(
                [
                    "docker",
                    "inspect",
                    "-f",
                    "{{.State.Running}}",
                    container_name,
                ],
                timeout=15,
            )
            if inspect_proc.stdout.strip() == "false":
                logs = _get_container_logs(container_name)
                result.run_passed = False
                err_reason, solution = diagnose_runtime_failure(logs)
                result.error_message = f"Container exited unexpectedly during startup. {err_reason}"
                result.solution = solution
                result.log_tail = logs
                return result

            # Probe endpoints.
            #
            # 404 is NOT accepted. It proves only that something speaks HTTP
            # on the port, not that the app mounted: a container whose app
            # failed to start 404s on every path, and calling that
            # "accessible" is the false PASS this gate exists to prevent.
            # 401/403 ARE accepted -- refusing a request means the route
            # exists and was routed to.
            for path in probe_paths:
                status, _ = probe_http(f"{base_url}{path}", timeout=3)
                if status is not None and (
                    200 <= status < 400 or status in (401, 403)
                ):
                    # A 200 is conclusive — stop here and prefer it.
                    if status == 200:
                        serving_endpoint = f"{path} (HTTP {status})"
                        break
                    # 3xx/401/403: the server is routing. Hold it as a
                    # candidate but keep probing in case a later path 200s.
                    if serving_endpoint is None:
                        serving_endpoint = f"{path} (HTTP {status})"

            if serving_endpoint and "(HTTP 200)" in serving_endpoint:
                break

            time.sleep(2)

        if not serving_endpoint:
            logs = _get_container_logs(container_name)
            result.run_passed = False
            result.error_message = f"Service inside container did not become accessible on port {port} within {probe_timeout}s."
            result.solution = (
                f"Verify that the container's CMD/ENTRYPOINT starts the server listening on host 0.0.0.0 port {port}, "
                "and that lifespan or startup events complete without hanging."
            )
            result.log_tail = logs
            return result

        result.run_passed = True
        result.accessible_endpoint = serving_endpoint
        print(f"[PASS] Service is accessible: {serving_endpoint}")
        return result

    finally:
        # Cleanup container and image
        run_cmd(
            ["docker", "rm", "-f", container_name],
            timeout=DEFAULT_DOCKER_TIMEOUT,
        )
        run_cmd(
            ["docker", "rmi", "-f", image_tag],
            timeout=DEFAULT_DOCKER_TIMEOUT,
        )


def _get_container_logs(name: str, max_lines: int = 40) -> str:
    """Fetch stdout and stderr logs from a container."""
    proc = run_cmd(
        ["docker", "logs", "--tail", str(max_lines), name],
        timeout=DEFAULT_DOCKER_TIMEOUT,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    return out.strip() or "No logs recorded from container."


def find_recipes_with_dockerfile(
    root: Path = REPO_ROOT,
) -> list[Path]:
    """Find all recipe directories under core/, contrib/, skills/ with a root Dockerfile."""
    recipes: list[Path] = []
    for root_name in RECIPE_ROOTS:
        root_dir = root / root_name
        if not root_dir.is_dir():
            continue
        for manifest_path in root_dir.rglob("manifest.yaml"):
            recipe_dir = manifest_path.parent
            if (recipe_dir / "Dockerfile").is_file():
                recipes.append(recipe_dir)
    return sorted(set(recipes))


def to_diagnostic(result: ValidationResult) -> Diagnostic | None:
    """Convert a failed ValidationResult into a structured Diagnostic."""
    if result.passed:
        return None

    recipe = result.recipe
    dockerfile = f"{recipe}/Dockerfile"

    if not result.build_passed:
        what = f"Docker image failed to build for {recipe}."
        why = "Every recipe with a root Dockerfile must build cleanly."
        how = (
            result.solution
            or "Ensure the Dockerfile builds locally with 'docker build'."
        )
        if result.log_tail:
            how = f"{how}\n\nBuild log tail:\n{result.log_tail}"
        return Diagnostic(
            check="docker-build",
            what=what,
            why=why,
            how=how,
            doc=Doc.DOCKER_BUILD,
            file=dockerfile,
        )

    # Runtime / serve failure
    what = f"Container service for {recipe} failed to start or serve."
    why = "Every recipe with a root Dockerfile must start and respond to HTTP probes on port 8080."
    how = (
        result.solution
        or "Check container startup logs and ensure the service listens on port 8080."
    )
    if result.log_tail:
        how = f"{how}\n\nContainer logs:\n{result.log_tail}"

    return Diagnostic(
        check="docker-serves",
        what=what,
        why=why,
        how=how,
        doc=Doc.DOCKER_SERVES,
        file=dockerfile,
    )


def _run() -> int:
    parser = argparse.ArgumentParser(
        description="Verify Docker image build and runtime accessibility for recipes."
    )
    parser.add_argument(
        "recipes",
        nargs="*",
        help="Recipe directory paths to validate.",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="Validate all recipes in the repository that contain a root Dockerfile.",
    )
    parser.add_argument(
        "--build-timeout",
        type=int,
        default=900,
        help="Maximum build time in seconds per recipe (default: 900).",
    )
    parser.add_argument(
        "--probe-timeout",
        type=int,
        default=90,
        help="Maximum container probe wait time in seconds (default: 90).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8080,
        help="Port to probe inside the container (default: 8080).",
    )

    args = parser.parse_args()

    # Determine which recipe directories to validate
    target_dirs: list[Path] = []
    if args.all:
        target_dirs = find_recipes_with_dockerfile(REPO_ROOT)
    elif args.recipes:
        for r in args.recipes:
            p = Path(r)
            if not p.is_absolute():
                p = REPO_ROOT / p
            if p.is_dir():
                target_dirs.append(p)
            else:
                print(f"[SKIP] Recipe directory not found: {r}")
    else:
        # Check if paths are piped via stdin
        try:
            if not sys.stdin.isatty():
                stdin_paths = [ln.strip() for ln in sys.stdin if ln.strip()]
                for r in stdin_paths:
                    p = Path(r)
                    if not p.is_absolute():
                        p = REPO_ROOT / p
                    if p.is_dir():
                        target_dirs.append(p)
        except (OSError, ValueError):
            pass

    if not target_dirs:
        print("[PASS] No recipe directories with Dockerfiles to validate.")
        return EXIT_OK

    # Filter for directories that contain a root Dockerfile
    docker_targets = [d for d in target_dirs if (d / "Dockerfile").is_file()]
    if not docker_targets:
        print(
            "[PASS] None of the specified recipe directories contain a root Dockerfile."
        )
        return EXIT_OK

    # Verify Docker is available
    docker_check = run_cmd(
        ["docker", "info", "--format", "{{.ServerVersion}}"], timeout=15
    )
    if docker_check.returncode != 0:
        return report_infra_fault(
            infra_fault(
                CHECKER,
                "Docker daemon is not reachable on this runner. "
                f"Detail: {docker_check.stderr.strip() or docker_check.stdout.strip()}",
            )
        )

    diagnostics: list[Diagnostic] = []
    results: list[ValidationResult] = []

    for recipe_dir in docker_targets:
        res = validate_recipe_docker(
            recipe_dir,
            build_timeout=args.build_timeout,
            probe_timeout=args.probe_timeout,
            port=args.port,
        )
        results.append(res)
        diag = to_diagnostic(res)
        if diag:
            diagnostics.append(diag)

    print("\n" + "=" * 60)
    print("  Docker Build & Accessibility Validation Summary")
    print("=" * 60)
    for res in results:
        status_str = "PASS" if res.passed else "FAIL"
        endpoint_info = (
            f" ({res.accessible_endpoint})" if res.accessible_endpoint else ""
        )
        print(f"[{status_str}] {res.recipe}{endpoint_info}")

    return report(
        diagnostics,
        header="Recipe Dockerfile validation failed",
        passed_message="All recipe Dockerfiles built and verified accessible.",
        next_step="Fix the Dockerfile or runtime configuration for each failed recipe listed above.",
    )


def main() -> int:
    return _run()


if __name__ == "__main__":
    sys.exit(guard(CHECKER, main))
