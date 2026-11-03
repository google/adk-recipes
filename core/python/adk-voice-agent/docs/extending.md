# Extending ADK Voice Agent

Each extension point is a small protocol with a reference implementation to copy.

In this doc:

- [Add a coding harness](#add-a-coding-harness)
- [Add an app](#add-an-app)
- [Make any tool non-blocking](#make-any-tool-non-blocking)
- [Add an answer card](#add-an-answer-card)
- [Add a computer provider](#add-a-computer-provider)
- [Change what needs approval](#change-what-needs-approval)
- [Customize the computer image](#customize-the-computer-image)

## Add a coding harness

Implement `CodingHarness` ([`app/workers/harnesses/base.py`](../app/workers/harnesses/base.py)) or subclass `CliHarness` (see [`claude.py`](../app/workers/harnesses/claude.py) and [`antigravity.py`](../app/workers/harnesses/antigravity.py)) and install the CLI in [`sandbox/Dockerfile`](../sandbox/Dockerfile):

| Method | Returns |
|---|---|
| `build_argv(req)` | CLI command with prompt, session ID (or `--resume`), and model |
| `parse_line(line, state)` | `HarnessEvent`s (progress, activity, result) from stdout |
| `thread_files(req)` | Per-thread config files (instructions, `${VOICE_AGENT_MCP_URL}` / `${VOICE_AGENT_MCP_TOKEN}` MCP config, native `permissions.deny` rules via `permission_settings()` / `deny_rules()`), never raw tokens |

Register it in `_built_ins()` in [`registry.py`](../app/workers/harnesses/registry.py), link `~/.agents/skills` in [`sandbox/entrypoint.sh`](../sandbox/entrypoint.sh) if needed, test with [`tests/fake_agent_cli.py`](../tests/fake_agent_cli.py), and run `make eval-sandbox HARNESS=my_agent`.

## Add an app

1. **Declare it** under `apps` in [`config/integrations.yaml`](../config/integrations.yaml) (`AuthProvider` and `app_for_server()` in [`providers.py`](../app/auth/providers.py); surfaced by [`app/api/apps.py`](../app/api/apps.py) → `apps_state()`).
2. **Wire its MCP servers** in [`.mcp.json`](../.mcp.json) (proxied by [`mcp_gateway.py`](../app/api/mcp_gateway.py); stdio servers live in [`workspace_mcp/`](../workspace_mcp/)) and document usage in [`sandbox/skills/connected-apps-mcp/SKILL.md`](../sandbox/skills/connected-apps-mcp/SKILL.md).
3. **Add optional hooks** via `ProviderHooks` in [`app/auth/provider_hooks.py`](../app/auth/provider_hooks.py) (see [`app/auth/github/provider.py`](../app/auth/github/provider.py) → `GitHubHooks`) and `IDENTITY_PROBES` in [`app/auth/verify.py`](../app/auth/verify.py).
4. **Register in onboarding** ([`scripts/onboard.py`](../scripts/onboard.py), [`scripts/provisioning/github_app.py`](../scripts/provisioning/github_app.py), `get_redirect_uri()` in [`providers.py`](../app/auth/providers.py), [`.env.example`](../.env.example), and [configuration](configuration.md#connected-apps)).
5. **Gate external actions** in `mcp_gates` in [`policy.yaml`](../config/policy.yaml) ([Change what needs approval](#change-what-needs-approval)).
6. **Test** in [`tests/fake_integrations.py`](../tests/fake_integrations.py), [`tests/unit/test_mcp_gateway.py`](../tests/unit/test_mcp_gateway.py), and [`tests/unit/test_apps_config.py`](../tests/unit/test_apps_config.py).

## Make any tool non-blocking

```python
from app.voice.non_blocking import non_blocking_tool


async def summarize_logs(service: str) -> str:
    """Summarize the last hour of logs for a service."""
    ...


root_agent = Agent(..., tools=[non_blocking_tool(summarize_logs)])
```

Use `thread_tool()` when the tool starts or steers a background thread.

## Add an answer card

Answer cards are served by [`app/api/surfaces.py`](../app/api/surfaces.py) → `build_a2ui_surfaces()` and validated by [`app/context_cards.py`](../app/context_cards.py) → `CARD_KINDS` and `card_items()`:

1. Add the kind and allowed fields to `CARD_KINDS` in [`app/context_cards.py`](../app/context_cards.py).
2. Render it in [`web/src/components/a2ui/answer/AnswerCard.tsx`](../web/src/components/a2ui/answer/AnswerCard.tsx) ([`web/src/lib/cards.ts`](../web/src/lib/cards.ts)), shared by [`A2UISurfaceDeck.tsx`](../web/src/components/a2ui/A2UISurfaceDeck.tsx) and [`web/src/components/stage/ThreadView.tsx`](../web/src/components/stage/ThreadView.tsx).

New thread states go in `TRANSITIONS` in [`app/threads/model.py`](../app/threads/model.py).

## Add a computer provider

Run the computer image ([`sandbox/server.py`](../sandbox/server.py), [`sandbox/README.md`](../sandbox/README.md#endpoints-serverpy)) with a persistent volume at `/home/agent` and implement `ComputerProvider` ([`app/workers/providers/base.py`](../app/workers/providers/base.py)) returning an `Environment` ([`app/workers/environment.py`](../app/workers/environment.py)):

| Method | Purpose |
|---|---|
| `name`, `ensure(user_id)`, `status(user_id)`, `stop(user_id)`, `known_users()` | Core lifecycle (`ComputerStatus`: `missing`, `starting`, `running`, `stopping`, `stopped`, `error`) |
| `IdleStoppable.running_environment(user_id)` | Optional: return client only if already running (for reaper and warm hours) |
| `ProviderNotices.pop_notice(user_id)` | Optional: surface one lifecycle notice on the thread |

Use `sandbox_owner()` / `owner_id()` from [`common.py`](../app/workers/providers/common.py), raise `ComputerUnavailableError` on misconfiguration, register in [`registry.py`](../app/workers/providers/registry.py) (`_FACTORIES` or `register_provider()`), document settings in [`.env.example`](../.env.example) and [configuration](configuration.md#where-coding-threads-run), and copy from [`static.py`](../app/workers/providers/static.py), [`docker.py`](../app/workers/providers/docker.py), or [`gce.py`](../app/workers/providers/gce.py) (tested in [`tests/unit/test_providers.py`](../tests/unit/test_providers.py)).

## Change what needs approval

Edit `mcp_gates.ask` or `permissions.deny` in [`config/policy.yaml`](../config/policy.yaml) (or `~/.adk-voice-agent/policy.yaml`), loaded by [`app/workers/policy.py`](../app/workers/policy.py), and test in [`tests/unit/test_policy.py`](../tests/unit/test_policy.py).

## Customize the computer image

Update [`sandbox/Dockerfile`](../sandbox/Dockerfile) ([`sandbox/`](../sandbox/), [`sandbox/README.md`](../sandbox/README.md)), build with `make computer-image`, and roll out the new tag via `computer_image` in Terraform.

## Where to go next

- [Architecture](architecture.md).
- [Security model](security-model.md).
- [Evals](evals.md).
- [Configuration](configuration.md).
