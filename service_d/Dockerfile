FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data

WORKDIR /app
RUN groupadd -r appuser && useradd -r -g appuser appuser && mkdir -p /data && chown appuser:appuser /data

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY d ./d
COPY config ./config
COPY rules ./rules

USER appuser
EXPOSE 8400
HEALTHCHECK --interval=5s --timeout=2s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8400/healthz').status == 200 else 1)"
CMD ["python", "-m", "d.main"]
