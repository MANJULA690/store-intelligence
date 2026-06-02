FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends curl && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir fastapi==0.115.0 "uvicorn[standard]==0.32.0" sqlalchemy==2.0.36 pydantic==2.9.2 python-multipart==0.0.12 structlog==24.4.0 python-dateutil==2.9.0.post0

COPY app/ ./app/

RUN mkdir -p /data /app/events

ENV DB_PATH=/data/store_intelligence.db
ENV PYTHONUNBUFFERED=1

HEALTHCHECK --interval=15s --timeout=5s --start-period=10s --retries=3 CMD curl -sf http://localhost:8000/health || exit 1

EXPOSE 8000

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
