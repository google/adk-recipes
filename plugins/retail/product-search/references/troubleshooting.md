# Troubleshooting

Read this when `setup.py` or a per-step script exits non-zero. Match
the error message against the table below before guessing.

## Error table

| Error pattern | Likely cause | Fix |
|---|---|---|
| `BILLING_DISABLED` / `Billing must be enabled` | GCP project has no billing account | Link a billing account in Cloud Console, then re-run |
| `PERMISSION_DENIED` on BigQuery | Service account missing IAM | `gcloud projects add-iam-policy-binding $PROJECT_ID --member=user:$EMAIL --role=roles/bigquery.dataEditor` |
| `PERMISSION_DENIED` on Vertex AI | Missing `aiplatform.user` role | `gcloud projects add-iam-policy-binding $PROJECT_ID --member=user:$EMAIL --role=roles/aiplatform.user` |
| `API has not been used` / `is disabled` | Required API not enabled | `gcloud services enable bigquery.googleapis.com aiplatform.googleapis.com` |
| `Collection not found` at query time | Vector Search collection never created or wrong path | Re-run `ingest_vertex_search.py`; verify `VECTOR_SEARCH_COLLECTION` env var |
| `setup.py` exits with `AttributeError: 'NoneType' object has no attribute 'get'` from `_setup_utils.py` | `design-spec.md` was rewritten as plain Markdown instead of editing the YAML template bootstrap copied. `_setup_utils.load_config()` returns an empty dict for non-frontmatter files, but downstream code then indexes into a nested dict that isn't there and the error surfaces one call deeper | Wait for `bootstrap.sh` to finish before touching `./design-spec.md`. Then **edit** the file (don't rewrite) — modify only the values inside the existing `---...---` frontmatter block. The canonical template is at `<install-dir>/assets/design-spec.md` |
| `adk web` boots but `/list-apps` returns `[]`, browser shows "No agents found in current folder" | Bare `adk` resolved through `PATH` to a global Python (pyenv, brew, etc.) that doesn't have the skill's editable install on `sys.path`. The server runs but can't import the agent module | Kill it (`lsof -ti :8765 \| xargs kill -9`) and restart with the workspace venv's adk explicitly: `.venv/bin/adk web "$SKILL_DIR/scripts" --port 8765`. Same root cause if `python "$SKILL_DIR/scripts/setup.py"` succeeds but the smoke test fails — use `.venv/bin/python` instead of bare `python` |
| `VECTOR_SEARCH_COLLECTION is malformed` | env var has embedded whitespace (usually a newline from a wrapped paste) | Re-`export` the value on a single line; prefer the `$PROJECT_ID` short form |
| `MethodNotImplemented: 501` from Vector Search in `us-central1` | Almost always a malformed `VECTOR_SEARCH_COLLECTION` (see above) -- not a real region/service issue | Echo `$VECTOR_SEARCH_COLLECTION` and confirm it has no `\n` or whitespace |
| `ModuleNotFoundError: google.cloud.aiplatform` | Python deps not installed | `pip install -e .` from the skill dir, or `pip install google-cloud-aiplatform google-cloud-bigquery google-genai` |
| `Package 'retail-product-search' requires a different Python: 3.10.X not in '>=3.11'` | venv was created with a Python older than 3.11 | Recreate the venv with a 3.11+ interpreter: `rm -rf .venv && python3.12 -m venv .venv && source .venv/bin/activate && bash -c "pip install -e '$SKILL_DIR'"` (substitute the Python version you have installed). Use the interpreter-detection loop from the Workspace Setup section to auto-find one. |
| `ModuleNotFoundError: google.adk` | Editable install never ran, or venv is stale | `bash -c "pip install -e '$SKILL_DIR'"` — google-adk is an unconditional dependency, no `[adk]` extra needed |
| `Schema mismatch` from `validate_schema.py` | CSV missing required fields (`product_id`, `name`, `price`) | Add the missing columns and re-run; or pass `--fields-level Standard` if your data is sparse |
| `Quota exceeded` on embedding requests | Free-tier embedding quota hit | Wait an hour, re-run with a smaller catalog slice, or request a quota increase. The script catches per-row failures and continues, so a partial run is fine to resume |
| Setup script hangs on Vector Search create | Collection creation is async and takes 2-5 min | Wait. If >10 min with no progress, check Cloud Console > Vertex AI > Vector Search for collection status |
| `projects//locations/...` in error path | `$GOOGLE_CLOUD_PROJECT` was empty when you ran the `export VECTOR_SEARCH_COLLECTION=...` -- substitution produced an empty project ID | `export GOOGLE_CLOUD_PROJECT=<your-project>` first, then re-run the `export VECTOR_SEARCH_COLLECTION` line, then restart `adk web` |
| Agent in `adk web` keeps saying "tool encountered an error" / "I am still unable to search" even after env fixed | ADK session memory -- model learned the tool is broken from earlier turns | Click "New Session" in the ADK web UI; the fresh session will retry the tool |
| Agent ignores price/currency filters in user queries | Retriever is pure semantic similarity; no structured filters at retrieval time | Expected behavior. Document the limit in your demo; or add a `currency`/`price_eur` column to the catalog and prompt the agent to filter client-side |

## ADK web UI: "tool encountered an error" loop

If `adk web` returns "tool encountered an error" repeatedly even after
the env var is correct: ADK persists session history in
`scripts/.adk/session.db`. If the first few queries in a session got a
tool error (typically a malformed env var, since fixed), the model
"learns" the tool is broken and starts skipping it, apologizing without
retrying.

Click "New Session" in the ADK web UI after fixing the underlying
issue -- a fresh session will retry the tool from scratch.
