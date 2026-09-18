FROM python:3.13-slim

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN pip install --no-cache-dir uv \
    && uv sync --frozen --no-dev --no-install-project

COPY migrations ./migrations
COPY alembic.ini ./
COPY src ./src
RUN uv pip install --system --no-deps .

ENV PYTHONUNBUFFERED=1
ENV PYTHONPATH=/app/src
