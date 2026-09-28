FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        nodejs \
        npm \
        procps \
        ripgrep \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir \
        fastapi==0.115.0 \
        pydantic==2.9.2 \
        "uvicorn[standard]==0.30.6" \
        uv==0.11.21

WORKDIR /opt/runtime
COPY protocol.py server.py entrypoint.sh /opt/runtime/

RUN useradd --create-home --uid 1000 --shell /bin/bash sandbox \
    && mkdir -p /workspace \
    && chmod +x /opt/runtime/entrypoint.sh \
    && chown -R sandbox:sandbox /workspace /opt/runtime

USER sandbox
WORKDIR /workspace

# On-demand CLIs install into ~/.local/bin. They can't ride the /workspace
# migration anyway (it drops symlinks and strips +x), so they live in $HOME and
# are reinstalled after a runtime upgrade; only their *credentials* go on
# /workspace (the installing skill's concern). The terminal tool runs a
# non-login /bin/sh (no ~/.profile), so put ~/.local/bin on PATH here. This is
# the one generic, tool-agnostic fact the image owns.
ENV PATH=/home/sandbox/.local/bin:$PATH

EXPOSE 8080
CMD ["/opt/runtime/entrypoint.sh"]
