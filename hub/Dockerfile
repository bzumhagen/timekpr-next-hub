FROM python:3.12-slim

WORKDIR /app

COPY hub/requirements.txt /app/hub-requirements.txt
COPY core /app/core
RUN pip install --no-cache-dir -r /app/hub-requirements.txt

COPY hub/timekpr_hub /app/timekpr_hub
COPY hub/migrations /app/migrations
COPY hub/alembic.ini /app/alembic.ini

ENV PYTHONPATH=/app/core:/app

EXPOSE 8000

CMD ["sh", "-c", "alembic upgrade head && uvicorn timekpr_hub.app:app --host 0.0.0.0 --port 8000"]
