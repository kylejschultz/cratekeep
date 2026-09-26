FROM python:3.12-slim

ARG BUILD_SHA=unknown

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY beets_mvp ./beets_mvp

RUN useradd --create-home --uid 10001 app \
    && mkdir -p /data/inbox /data/library /data/config \
    && chown -R app:app /data /app
USER app

ENV STATE_PATH=/data/config
ENV BUILD_SHA=${BUILD_SHA}
EXPOSE 8788
CMD ["gunicorn", "--bind=0.0.0.0:8788", "--workers=1", "--threads=4", "beets_mvp:create_app()"]
