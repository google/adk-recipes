.PHONY: install lint lint-check test demo standalone build clean tf-fmt tf-check format-ui format-ui-check lint-ui

install:
	uv sync --dev --extra lint --frozen

TY_PATHS := ./src ./ui ./tests ./extension ./tools/test_*.py

# The Python files published to adk-recipes, whose CI type-checks them with
# mypy. `uvx` runs mypy outside the project environment, as that CI does, so
# third-party imports resolve as missing and are ignored in both places.
MYPY_PATHS := $(shell git ls-files --cached --others --exclude-standard -- \
	'src/*.py' 'extension/*.py' 'tests/*.py' 'tools/*.py' 'ui/*.py' \
	'skills/ask-aqa-agent/*.py')
MYPY := uvx mypy --ignore-missing-imports --follow-imports=silent \
	--disable-error-code=annotation-unchecked

lint:
	uv sync --dev --extra lint
	uv run python tools/ci/check_conventions.py --fix
	uv run ruff check . --fix
	uv run ruff format .
	uv run ty check $(TY_PATHS)
	@echo "$(MYPY) <$(words $(MYPY_PATHS)) exported files>"
	@$(MYPY) $(MYPY_PATHS)

lint-check:
	uv sync --dev --extra lint --locked
	uv run python tools/ci/check_conventions.py
	uv run ruff check .
	uv run ruff format . --check
	uv run ty check $(TY_PATHS)
	@echo "$(MYPY) <$(words $(MYPY_PATHS)) exported files>"
	@$(MYPY) $(MYPY_PATHS)

# Formats and lints the dashboard frontend with Biome; the scripts in
# ui/web/package.json define the version and file coverage.
format-ui:
	npm --prefix ui/web run format

format-ui-check:
	npm --prefix ui/web run format:check

lint-ui:
	npm --prefix ui/web run lint

# Discovers tests using `testpaths` configured in pyproject.toml.
test:
	uv run pytest

# The whole dashboard against a generated month of data: no GCP project, no
# deployment, no model. See tools/mock_ui.sh for the scenario knobs, including
# the two that generate a deployment with nothing to report.
#
# The build is not optional here: ui/static_v2 is gitignored and nothing but
# the image's Node stage produces it, so without it `app.py` has no front end
# to serve and says so instead.
demo: ui/web/node_modules
	npm --prefix ui/web run build
	tools/mock_ui.sh

# Runs AQuA locally against a deployed agent without deploying AQuA infrastructure.
# Uses live models and in-process storage rather than mock data.
#
# Requires Application Default Credentials (ADC). Loads configuration from
# shell variables and `src/ambient_quality_agent/.env` (see standalone in `.env.example`).
# Validates configuration first to fail immediately before npm build steps.
standalone:
	@AQA_STANDALONE=1 uv run python tools/check_config.py
	@$(MAKE) --no-print-directory ui/web/node_modules
	@echo "Building the dashboard; a minute or so on a cold cache."
	npm --prefix ui/web run build
	AQA_STANDALONE=1 tools/local_ui.sh

# `npm ci` deletes node_modules and reinstalls it, which is ~30s, so make it
# conditional on the lockfile having actually moved. The touch is what makes
# that comparison work: `npm ci` leaves the directory's mtime *behind* the
# lockfile's, so without it every single run reinstalls.
ui/web/node_modules: ui/web/package-lock.json
	npm --prefix ui/web ci
	@touch ui/web/node_modules

build:
	uv build

clean:
	rm -rf dist/ build/ *.egg-info site/

tf-fmt:
	terraform fmt -recursive terraform

# The local run of what the terraform-check workflow does. `-backend=false`
# keeps it credential-free: validate needs the provider schema, not state.
tf-check:
	terraform fmt -check -recursive terraform
	@for dir in terraform/bootstrap terraform/modules/aqa terraform/examples/single-project terraform/examples/attach; do \
		echo "==> $$dir"; \
		terraform -chdir=$$dir init -backend=false -input=false >/dev/null || exit 1; \
		terraform -chdir=$$dir validate || exit 1; \
	done

# Optional local additions; skipped when the file does not exist.
-include tools_internal/internal.mk
