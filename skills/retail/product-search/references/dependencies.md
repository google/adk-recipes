# Dependencies

`pip install -e .` from the install dir resolves everything from
`pyproject.toml`. You don't install these manually.

## Required

- `google-adk>=2.2.0` — ADK web UI and agent runtime
- `google-cloud-aiplatform>=1.30`
- `google-cloud-bigquery>=3.0`
- `google-cloud-storage>=2.0`
- `google-cloud-vectorsearch>=0.5,<1.0` (preview, pinned)
- `google-genai>=1.0`
- `pyOpenSSL>=23.0` (mTLS for BigQuery in some envs)
- `python-dotenv>=1.0` — loads `.env` at import via `scripts/config.py`
- `pyyaml>=6.0`
- `requests>=2.28`

## Install quirks (handled by bootstrap.sh)

1. **Paths with spaces.** `bash -c "pip install -e '$SKILL_DIR'"` quotes the
   whole argument so pip sees one path.
2. **Agent shell tools reset state between calls.** Run the workspace setup
   block as a single shell invocation.
3. **Stripped PATH** in sandboxed terminals hides brew/pyenv Python. The
   bootstrap script falls back to absolute paths
   (`/opt/homebrew/bin/python3.13`, `~/.pyenv/shims/python3.12`, etc.).
