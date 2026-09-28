FROM ghcr.io/astral-sh/uv:python3.11-bookworm-slim

WORKDIR /app

COPY pyproject.toml uv.lock ./
COPY app/ ./app/

RUN uv sync --frozen --no-dev

EXPOSE 8080

CMD ["uv", "run", "python", "app/fast_api_app.py"]
