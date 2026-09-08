# Single process: APScheduler fires the digest, Socket Mode listens for DMs.
# No web server, so no port is exposed — the app dials out to Slack.
FROM python:3.11-slim

COPY --from=ghcr.io/astral-sh/uv:0.5 /uv /usr/local/bin/uv

WORKDIR /app
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# Dependencies first, in their own layer, so editing source does not
# reinstall the world on every deploy.
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ src/
RUN uv sync --frozen --no-dev

# Don't run as root.
RUN useradd --create-home --uid 1000 app && chown -R app:app /app
USER app

CMD ["uv", "run", "--no-sync", "python", "-m", "clickup_slack_agent"]
