# Elderly API — Cloud Run image.
# No credentials are baked in: on Cloud Run the service account identity (ADC) is used,
# and the Gemini key comes from Secret Manager as an env var.
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.7.6 /uv /bin/uv

WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1 \
    PATH="/app/.venv/bin:$PATH"

# Dependencies first (cached layer), then the app.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app

# Run as an unprivileged user.
RUN useradd --system --uid 10001 app && chown -R app /app
USER app

# Cloud Run sets $PORT. --proxy-headers: trust Google's front end for scheme/client IP.
CMD exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8080}" --proxy-headers --forwarded-allow-ips="*"
