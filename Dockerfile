FROM python:3.12-slim AS base
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

COPY pyproject.toml README.md alembic.ini ./
COPY src ./src
COPY migrations ./migrations
COPY docker/entrypoint.sh /entrypoint.sh
RUN pip install ".[postgres]" && sed -i 's/\r$//' /entrypoint.sh && chmod +x /entrypoint.sh \
    && useradd --system --uid 10001 --no-create-home auditor
USER auditor

EXPOSE 8000
ENTRYPOINT ["/entrypoint.sh"]
CMD ["api"]
