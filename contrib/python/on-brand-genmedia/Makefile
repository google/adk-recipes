export UV_INDEX_URL=https://pypi.org/simple
export UV_NO_CONFIG=1

.PHONY: install delete test

install:
	uv sync

delete:
	uv run python deployment/deploy.py delete

test:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	rm -rf .pytest_cache
	uv run pytest --cache-clear
