# Milestone 2: headless danser rendering (Xvfb + Mesa llvmpipe, CPU-only).
# danser binary is baked in from a pinned upstream release.
ARG DANSER_VERSION=0.11.0
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    DISPLAY=:99

WORKDIR /app

# System deps: Xvfb + software GL for headless danser, ffmpeg for danser recording,
# unzip for the danser release archive.
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb \
        mesa-utils \
        libgl1 \
        libgl1-mesa-dri \
        libglu1-mesa \
        libx11-6 \
        libxrandr2 \
        libxinerama1 \
        libxcursor1 \
        libxi6 \
        libgtk-3-0 \
        ffmpeg \
        unzip \
        ca-certificates \
        curl \
    && rm -rf /var/lib/apt/lists/*

# Pinned danser release -> /opt/danser (contains danser-cli).
RUN mkdir -p /opt/danser \
    && curl -fsSL -o /tmp/danser.zip \
        "https://github.com/Wieku/danser-go/releases/download/${DANSER_VERSION}/danser-${DANSER_VERSION}-linux.zip" \
    && unzip -q /tmp/danser.zip -d /opt/danser \
    && rm /tmp/danser.zip \
    && chmod +x /opt/danser/danser-cli \
    && ls -la /opt/danser

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY pyproject.toml ./
COPY src/ ./src/
COPY config/ ./config/
COPY scripts/ ./scripts/
COPY danser/ ./danser/

RUN pip install --no-cache-dir -e . \
    && chmod +x scripts/entrypoint.sh \
    && mkdir -p /opt/danser/settings /opt/danser/videos \
    && cp ./danser/settings/pipeline.json /opt/danser/settings/pipeline.json \
    && useradd --create-home --uid 1000 app \
    && mkdir -p /data/db /data/working /data/rendered /data/daily /data/beatmaps/songs \
    && chown -R app:app /app /data /opt/danser

USER app

ENTRYPOINT ["scripts/entrypoint.sh"]
CMD ["discover"]
