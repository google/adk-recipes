# Tests

Python, under pytest — `make test`, or `uv run pytest`. CI runs the same
suite with `-n auto`.

The front end has its own suite, under vitest, beside the code it covers:
`npm --prefix ui/web run check`, which CI also runs. See
[`ui/web/README.md`](../ui/web/README.md).
