FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    UV_NO_CACHE=1 \
    PATH="/app/.venv/bin:${PATH}"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl git gh openssh-client \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.9.17 /uv /uvx /usr/local/bin/

WORKDIR /app

COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY configs ./configs
COPY docs ./docs
COPY skills ./skills
COPY model-policy.yml engineering-loop-policy.yml ./

RUN uv sync --frozen --no-dev

ENTRYPOINT ["hyrule-engineering-loop"]
CMD ["--help"]
