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

"""SSH into your own computer through IAP (`make ssh`, `make doctor`).

Starting a stopped computer is left to `GceProvider.ensure()` so an outdated VM is
recreated from the current instance template rather than started in place.
"""

from __future__ import annotations

import argparse
import os
import subprocess

from app.workers.providers.base import ComputerState
from app.workers.providers.common import sandbox_owner
from app.workers.providers.gce import instance_name, instance_state
from scripts.deploy import deploy_config, fail, gcloud_account

NOT_RUNNING = {
    ComputerState.MISSING: "You don't have a computer yet; open the app to create it.",
    ComputerState.STARTING: "Your computer is starting; try again in a minute.",
    ComputerState.STOPPING: "Your computer is stopped; open the app to start it.",
    ComputerState.STOPPED: "Your computer is stopped; open the app to start it.",
}


def instance_status(name: str, project: str, zone: str) -> str | None:
    result = subprocess.run(  # noqa: S603 - fixed gcloud argv, no shell
        [  # noqa: S607 - gcloud resolved from PATH
            "gcloud",
            "compute",
            "instances",
            "list",
            f"--project={project}",
            f"--zones={zone}",
            f"--filter=name={name}",
            "--format=value(name,status)",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        fail(result.stderr.strip() or f"could not look up {name}.")
    for line in result.stdout.splitlines():
        row_name, _, status = line.partition("\t")
        if row_name == name:
            return status.strip()
    return None


def not_running_message(status: str | None) -> str | None:
    state = ComputerState.MISSING if status is None else instance_state(status)
    if state == ComputerState.RUNNING:
        return None
    return NOT_RUNNING.get(
        state, f"Your computer is {status}; open the app to check on it."
    )


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--project", required=True)
    parser.add_argument("--region", default="us-central1")
    parser.add_argument(
        "--var-file", help="terraform tfvars file of this deployment"
    )
    parser.add_argument("remote", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    account = gcloud_account().lower()
    if not account:
        fail("no gcloud account. Run 'gcloud auth login'.")
    config = deploy_config(args.project, args.region, args.var_file)
    zone = config["computer_zone"]
    name = instance_name(sandbox_owner(account), config["computer_deployment"])
    if message := not_running_message(
        instance_status(name, args.project, zone)
    ):
        raise SystemExit(message)
    remote = args.remote[1:] if args.remote[:1] == ["--"] else args.remote
    command = [
        "gcloud",
        "compute",
        "ssh",
        name,
        f"--project={args.project}",
        f"--zone={zone}",
        "--tunnel-through-iap",
        "--quiet",
    ]
    os.execvp("gcloud", [*command, "--", *remote] if remote else command)  # noqa: S606, S607


if __name__ == "__main__":
    main()
