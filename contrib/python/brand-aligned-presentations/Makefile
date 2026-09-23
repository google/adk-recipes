.PHONY: install backend update test

install:
	uv sync

backend:
	uv run python presentation_agent/deployment/deploy.py --mode create

update:
	uv run python presentation_agent/deployment/deploy.py --mode update

test:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	rm -rf .pytest_cache
	uv run pytest --cache-clear