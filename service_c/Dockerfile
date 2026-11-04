FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SERVICE_C_HOST=0.0.0.0 \
    SERVICE_C_PORT=8080 \
    SERVICE_C_DB_PATH=/data/news_c.db

WORKDIR /app

# Create non-root user and /data volume directory (Decision D10)
RUN groupadd -r appuser && useradd -r -g appuser appuser \
    && mkdir -p /data /app/service_c \
    && chown -R appuser:appuser /data /app

COPY requirements.txt /app/service_c/requirements.txt
RUN pip install --no-cache-dir -r /app/service_c/requirements.txt

COPY . /app/service_c/
RUN chown -R appuser:appuser /app/service_c

USER appuser
EXPOSE 8080

HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)"

CMD ["uvicorn", "service_c.main:app", "--host", "0.0.0.0", "--port", "8080"]
