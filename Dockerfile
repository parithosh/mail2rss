FROM ghcr.io/astral-sh/uv:python3.13-bookworm-slim AS builder

WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY src ./src
COPY REQUIREMENTS.MD ./
RUN uv sync --no-dev --frozen

FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY config.example.toml /app/config.example.toml
ENV PATH="/app/.venv/bin:$PATH"

RUN groupadd --gid 1000 mail2rss \
  && useradd --uid 1000 --gid 1000 --home-dir /var/lib/mail2rss --create-home mail2rss \
  && mkdir -p /var/lib/mail2rss/feeds \
  && chown -R 1000:1000 /var/lib/mail2rss

USER 1000:1000
VOLUME ["/var/lib/mail2rss"]
ENTRYPOINT ["mail2rss"]
