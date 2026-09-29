#!/usr/bin/env bash
# Installs the Antigravity CLI (agy) into ~/.local/bin for the AI workflows.
#
# The installer always fetches the latest build; it has no version flag.
#
# The script is downloaded to a file rather than piped into bash because the
# CDN intermittently serves it gzipped without a Content-Encoding header, so
# curl cannot decode it and bash fails on the binary with "syntax error near
# unexpected token".
set -euo pipefail

installer="${RUNNER_TEMP:-$(mktemp -d)}/agy-install.sh"
# --max-time bounds each attempt, so a stalled connection is retried rather
# than hanging the job.
curl -fsSL --compressed --retry 3 --retry-all-errors \
  --connect-timeout 10 --max-time 60 \
  -o "${installer}" https://antigravity.google/cli/install.sh
if gzip -t "${installer}" 2>/dev/null; then
  mv "${installer}" "${installer}.gz"
  gunzip "${installer}.gz"
fi
bash "${installer}"
