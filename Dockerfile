# Lightweight Cloud Run Container for ADK Token Control Tower
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

# Copy and install package
COPY . /app
RUN pip install --no-cache-dir ".[bigquery]" \
    && useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app

USER appuser

EXPOSE 8080

# DNS-rebinding allow-list note:
# By default, only loopback Host/Origin headers matching $PORT are accepted.
# When publishing on a mapped host port or behind an HTTPS proxy / Cloud Run, pass
# TOKENOMICS_ALLOWED_HOSTS (comma-separated, e.g. -e TOKENOMICS_ALLOWED_HOSTS=localhost:9090
# for 'docker run -p 9090:8080' or TOKENOMICS_ALLOWED_HOSTS=*.run.app on Cloud Run).
# Run adk-tokenomics web server bound directly to $PORT
CMD ["sh", "-c", "exec adk-tokenomics web --host 0.0.0.0 --port ${PORT:-8080} --no-browser"]
