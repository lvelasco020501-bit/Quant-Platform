# Runtime image for the quant platform.
#
# The image ships only the application and its locked runtime dependencies. It contains no
# credentials: every secret is supplied at run time through the environment. The default
# command validates configuration rather than trading, so an accidentally started container
# cannot place an order.

FROM python:3.13-slim-bookworm AS base

COPY --from=ghcr.io/astral-sh/uv:0.12.0 /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=0 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=UTC

WORKDIR /app


FROM base AS builder

# Install dependencies first so that source edits do not invalidate the dependency layer.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-install-project --no-dev

COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev


FROM base AS runtime

RUN groupadd --system quant \
    && useradd --system --gid quant --home-dir /app --shell /usr/sbin/nologin quant

COPY --from=builder --chown=quant:quant /app/.venv /app/.venv
COPY --from=builder --chown=quant:quant /app/src /app/src
COPY --chown=quant:quant pyproject.toml README.md /app/

ENV PATH="/app/.venv/bin:${PATH}"

USER quant

ENTRYPOINT ["quantplatform"]
CMD ["check-config"]
