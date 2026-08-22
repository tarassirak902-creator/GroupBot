FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations

RUN python -m compileall -q src migrations \
    && pip install --upgrade pip \
    && pip install .

RUN useradd --create-home --uid 10001 groupbot && chown -R groupbot:groupbot /app
USER groupbot

CMD ["python", "-m", "groupbot.main"]
