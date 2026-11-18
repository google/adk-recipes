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

# The computer image: one per user, with that user's persistent volume mounted at
# /home/agent. Everything the image ships lives OUTSIDE /home/agent, so a fresh or
# years-old home works with any image version. The contract (layout, first boot,
# MCP relay, customizing) is in README.md. Port 8080 is the daemon (server.py).
#
# Bump a version by changing its ARG (or `--build-arg NAME=value`).
#
# Multi-arch: every step works on linux/amd64 and linux/arm64 (pinned checksums per
# architecture where a download differs). `docker build sandbox` builds for the local
# platform (arm64 on Apple Silicon, for the docker provider); `cloudbuild.yaml` builds
# and pushes both as one tag.

# --- Version pins -------------------------------------------------------------
ARG UV_VERSION=0.8.13
# Node 22 is the oldest major `@anthropic-ai/claude-code` supports.
ARG NODE_MAJOR=22
ARG NODE_VERSION=22.15.0
ARG NODE_SHA256_AMD64=29d1c60c5b64ccdb0bc4e5495135e68e08a872e0ae91f45d9ec34fc135a17981
ARG NODE_SHA256_ARM64=c3582722db988ed1eaefd590b877b86aaace65f68746726c1f8c79d26e5cc7de
# GitHub CLI release, and the sha256 of its Linux tarballs (gh_<v>_checksums.txt).
ARG GH_VERSION=2.102.0
ARG GH_SHA256_AMD64=bb766f710eef8ede859c18578c72c327597cd4c8a85b06001b1f3843c6019386
ARG GH_SHA256_ARM64=7862c86c72f43df3a2d93ddde6f473285b4e2af61b494849846827e513ef6484
# npm `@anthropic-ai/claude-code`.
ARG CLAUDE_CODE_VERSION=2.1.295
# npm `mcp-remote`: agy reaches the MCP gateway through it, since agy's MCP config
# can't read the gateway's URL and token from the environment itself.
ARG MCP_REMOTE_VERSION=0.15.2
# Antigravity CLI (`agy`) release, as `<version>-<build>` in its download path, and the
# sha512 of its Linux tarballs: the `url` and `sha512` of the installer's manifests,
# https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests/linux_<arch>.json
ARG AGY_RELEASE=1.4.7-5900205422411776
ARG AGY_SHA512_AMD64=beda760587589f5827b0b516c3ec2c5bd5cd2e91eb0e2533807ba1e990a27dcd63aef0191e0b76fb60feab0d16eaa8210fb79842c34dd4809e257e1712edd152
ARG AGY_SHA512_ARM64=6a9fa0df6300633404571f66ec7924098cc70a9f23ac55c3659139bbcbdd2ea5301fe1ef728d4c064fac65104ea14a2b74421f320a82e5bcf218c482b58e3a16

FROM ghcr.io/astral-sh/uv:${UV_VERSION} AS uv

FROM python:3.12-slim
ARG NODE_MAJOR
ARG NODE_VERSION
ARG NODE_SHA256_AMD64
ARG NODE_SHA256_ARM64
ARG GH_VERSION
ARG GH_SHA256_AMD64
ARG GH_SHA256_ARM64
ARG CLAUDE_CODE_VERSION
ARG MCP_REMOTE_VERSION
ARG AGY_RELEASE
ARG AGY_SHA512_AMD64
ARG AGY_SHA512_ARM64
SHELL ["/bin/bash", "-o", "pipefail", "-c"]

# System packages. Packages a user adds with `sudo apt-get` live in the
# container layer, so they are gone when the computer is recreated (image bump).
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl git gnupg jq less openssh-client procps ripgrep sudo tini unzip \
    && rm -rf /var/lib/apt/lists/*

# Node.js, from the pinned official tarball, checked against its pinned sha256.
RUN arch="$(dpkg --print-architecture)" \
    && case "$arch" in \
         amd64) node_arch="x64" want="$NODE_SHA256_AMD64" ;; \
         arm64) node_arch="arm64" want="$NODE_SHA256_ARM64" ;; \
         *) echo "no node checksum pinned for $arch" >&2; exit 1 ;; esac \
    && name="node-v${NODE_VERSION}-linux-${node_arch}" \
    && curl -fsSL -o /tmp/node.tgz "https://nodejs.org/dist/v${NODE_VERSION}/${name}.tar.gz" \
    && echo "${want}  /tmp/node.tgz" | sha256sum -c - \
    && tar -xzf /tmp/node.tgz -C /usr/local --strip-components=1 \
    && rm -f /tmp/node.tgz

COPY --from=uv /uv /uvx /usr/local/bin/

# GitHub CLI, from the pinned release tarball, checked against its pinned sha256.
RUN arch="$(dpkg --print-architecture)" \
    && case "$arch" in amd64) want="$GH_SHA256_AMD64" ;; arm64) want="$GH_SHA256_ARM64" ;; \
         *) echo "no gh checksum pinned for $arch" >&2; exit 1 ;; esac \
    && name="gh_${GH_VERSION}_linux_${arch}" \
    && curl -fsSL -o /tmp/gh.tgz "https://github.com/cli/cli/releases/download/v${GH_VERSION}/${name}.tar.gz" \
    && echo "${want}  /tmp/gh.tgz" | sha256sum -c - \
    && tar -xzf /tmp/gh.tgz -C /tmp \
    && install -m 0755 "/tmp/${name}/bin/gh" /usr/local/bin/gh \
    && rm -rf /tmp/gh.tgz "/tmp/${name}"

# The daemon's Python environment.
RUN uv venv /opt/venv \
    && uv pip install --python /opt/venv/bin/python --no-cache \
         "fastapi==0.141.1" "uvicorn[standard]==0.34.3" "httpx==0.28.1" "google-auth==2.58.0" "requests==2.34.2"

# Harness CLIs as npm globals under /usr/local.
RUN npm install -g --prefix /usr/local \
         "@anthropic-ai/claude-code@${CLAUDE_CODE_VERSION}" "mcp-remote@${MCP_REMOTE_VERSION}" \
    && npm cache clean --force

# Antigravity CLI, from the pinned release tarball, checked against its pinned sha512
# and version (`--version` runs with a throwaway HOME, since agy writes its config there).
RUN arch="$(dpkg --print-architecture)" \
    && case "$arch" in \
         amd64) path="linux-x64/cli_linux_x64.tar.gz" want="$AGY_SHA512_AMD64" ;; \
         arm64) path="linux-arm/cli_linux_arm64.tar.gz" want="$AGY_SHA512_ARM64" ;; \
         *) echo "no agy checksum pinned for $arch" >&2; exit 1 ;; esac \
    && curl -fsSL -o /tmp/agy.tgz "https://storage.googleapis.com/antigravity-public/antigravity-cli/${AGY_RELEASE}/${path}" \
    && echo "${want}  /tmp/agy.tgz" | sha512sum -c - \
    && tar -xzf /tmp/agy.tgz -C /tmp antigravity \
    && install -m 0755 /tmp/antigravity /usr/local/bin/agy \
    && test "$(HOME=/tmp/agy-home agy --version)" = "${AGY_RELEASE%%-*}" \
    && rm -rf /tmp/agy.tgz /tmp/antigravity /tmp/agy-home

# The user. Passwordless sudo, for installing system packages (see the caveat above).
RUN useradd --uid 1000 --user-group --shell /bin/bash --no-create-home --home-dir /home/agent agent \
    && mkdir -p /home/agent && chown agent:agent /home/agent \
    && echo "agent ALL=(ALL) NOPASSWD:ALL" > /etc/sudoers.d/agent && chmod 0440 /etc/sudoers.d/agent

# Seed files (copied into the home on boot without overwriting), the entrypoint, and the daemon.
COPY home/ /opt/computer/home/
COPY skills/ /opt/computer/skills/
COPY entrypoint.sh /opt/computer/entrypoint.sh
COPY server.py /opt/computer/server.py
RUN chmod 0755 /opt/computer/entrypoint.sh

# What the user installs (`uv tool install`, `npm install -g`, `pip install --user`)
# lands in ~/.local, on the volume, and comes first on PATH
# (app.workers.environment.SANDBOX_PATH_PREPEND). agy's background self-update is off,
# so the pinned version is the one that runs.
ENV HOME=/home/agent \
    SANDBOX_WORKSPACE=/home/agent \
    NPM_CONFIG_PREFIX=/home/agent/.local \
    AGY_CLI_DISABLE_AUTO_UPDATE=1 \
    PATH=/home/agent/.local/bin:/opt/venv/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin

WORKDIR /home/agent
EXPOSE 8080
# tini is PID 1 and reaps orphans: detached harness runs exit as orphans, and
# without a reaper they pile up as zombies. The entrypoint starts as root, prepares
# the home, then drops to `agent` and execs the daemon.
ENTRYPOINT ["tini", "--", "/opt/computer/entrypoint.sh"]
