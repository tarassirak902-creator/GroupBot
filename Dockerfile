FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --upgrade pip

FROM base AS runtime
RUN pip install .

COPY app ./app
COPY content ./content
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic

RUN useradd --create-home --uid 10001 groupbot && chown -R groupbot:groupbot /app
USER groupbot

CMD ["python", "-m", "app.main"]

FROM base AS test
RUN pip install ".[dev]"

COPY app ./app
COPY content ./content
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic
COPY tests ./tests

RUN useradd --create-home --uid 10001 groupbot && chown -R groupbot:groupbot /app
USER groupbot

CMD ["pytest", "-q"]
