Run `make baseline` to grade this agent with agents-cli. Then hill climb the agent with the google-agents-cli-eval skill until every train case passes.

Rules:
- Read `.agents-cli-spec.md` first. It is the contract the evals check.
- Only edit `app/agent.py`. Never edit anything under `tests/eval/`.
- Make one change per round, aimed at one root cause. After each change run `make round ROUND=round_N`, then `make compare A=<previous run> B=round_N`, and keep the change only if nothing regressed.
- When every train case passes, run `make heldout` once to check the fix generalizes.
- Finish by writing `agent_report.md` with the pass rate per round, what failed, and what you changed.
