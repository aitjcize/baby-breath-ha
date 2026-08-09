FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt \
    && groupadd --system detector \
    && useradd --system --gid detector --home-dir /app detector

COPY baby_respiration/app ./app
COPY config.example.yaml ./config.yaml
RUN chown -R detector:detector /app

USER detector
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2)" || exit 1

CMD ["python", "-m", "app", "--config", "/app/config.yaml"]

