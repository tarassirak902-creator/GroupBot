FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml ./
RUN pip install --upgrade pip && pip install .

COPY app ./app
COPY alembic.ini ./alembic.ini
COPY alembic ./alembic

RUN useradd --create-home --uid 10001 groupbot && chown -R groupbot:groupbot /app
USER groupbot

CMD ["python", "-m", "app.main"]
