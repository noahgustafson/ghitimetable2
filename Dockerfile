FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

ENV GHITIME_DATA=/data \
    GHITIME_DB=/data/ghitime.db \
    GHITIME_COOKIE_SECURE=1 \
    FLASK_APP=ghitime \
    TZ=America/Chicago

EXPOSE 8000

# migrations are forward-only and idempotent — safe on every start
CMD ["sh", "-c", "flask migrate && exec gunicorn -w 2 -b 0.0.0.0:8000 --access-logfile - 'ghitime:create_app()'"]
