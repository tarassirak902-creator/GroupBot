FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN pip install --upgrade pip

COPY pyproject.toml ./
COPY app ./app
COPY content ./content
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic

FROM base AS runtime
RUN pip install .

RUN useradd --create-home --uid 10001 groupbot && chown -R groupbot:groupbot /app
USER groupbot

CMD ["python", "-m", "app.main"]

FROM base AS test
COPY tests ./tests
RUN pip install ".[dev]"

RUN useradd --create-home --uid 10001 groupbot && chown -R groupbot:groupbot /app
USER groupbot

CMD ["pytest", "-q"]
