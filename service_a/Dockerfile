FROM python:3.12-slim AS build
WORKDIR /build
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

FROM python:3.12-slim
RUN useradd --create-home --uid 1000 lame
COPY --from=build /install /usr/local
WORKDIR /srv
COPY alembic.ini .
COPY migrations migrations
COPY config config
COPY app app
USER lame
EXPOSE 8000
# One worker: the rate limiter, breaker and shedding window live in this process's memory (D2).
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1"]
