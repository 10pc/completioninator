FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml ./
COPY src/ ./src/
COPY config/ ./config/
COPY scripts/ ./scripts/

RUN pip install --no-cache-dir -e . \
    && useradd --create-home --uid 1000 app \
    && mkdir -p /data/db /data/working /data/rendered /data/daily \
    && chown -R app:app /app /data

USER app

ENTRYPOINT ["scripts/entrypoint.sh"]
CMD ["discover"]
