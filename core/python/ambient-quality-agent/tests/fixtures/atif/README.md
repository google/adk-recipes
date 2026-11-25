# ATIF fixtures

Inputs for the `aqua_atif` tests (`tests/test_aqua_atif_*.py`).

- `sample_run.atif.json`: a minimal synthetic `ATIF-v1.8` run (`checkout-fixer`,
  two steps, one image, two tool calls) with one tool definition filled in.
- `complete_v1_7.atif.json`: a synthetic `ATIF-v1.7` run that uses most v1.7
  fields: embedded subagents, a subagent reference by path, images, the
  `is_error` tool-error convention, `context_management`, and a continuation.
- `harbor_terminus_2/`: Terminus-2 output copied unchanged from Harbor
  (https://github.com/harbor-framework/harbor) release v0.25.0,
  `tests/golden/terminus_2/`, licensed under the Apache License, Version 2.0;
  the license text is in `harbor_terminus_2/LICENSE`.
- `harbor_v0.25.0_models.diff`: the whole difference between Harbor v0.25.0's
  trajectory models and the copy in `src/aqua_atif/models/`.
