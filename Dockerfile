# Milestone 2: headless danser rendering (Xvfb + Mesa llvmpipe, CPU-only).
# danser binary is baked in from a pinned upstream release.
ARG DANSER_VERSION=0.11.0
# danser-grid fork ref (branch for M3; pin a tag at cutover).
ARG DANSER_GRID_REF=mvp/grid-spans

# ---- grid builder: danser-grid binary from the fork ----
FROM golang:1.24-bookworm AS gridbuilder
ARG DANSER_GRID_REF=mvp/grid-spans
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc \
        libgl1-mesa-dev \
        libglu1-mesa-dev \
        xorg-dev \
        libgtk-3-dev \
        ca-certificates \
        git \
    && rm -rf /var/lib/apt/lists/*
RUN git clone --depth 1 --branch ${DANSER_GRID_REF} https://github.com/10pc/danser-grid.git /src
WORKDIR /src
RUN go build -buildvcs=false -tags "exclude_cimgui_glfw exclude_cimgui_sdli" \
        -o /out/danser-grid . \
    && cp libbass.so libbass_fx.so libbassmix.so libyuv.so /out/ \
    && ls -la /out/

FROM python:3.12-slim
# Re-declare: pre-FROM ARGs are not visible in build steps without this.
ARG DANSER_VERSION=0.11.0

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
        fonts-dejavu-core \
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
    && mkdir -p /opt/danser/settings /opt/danser/videos /opt/danser/skins \
    && cp -r ./danser/settings/. /opt/danser/settings/ \
    && cp -r ./danser/skins/. /opt/danser/skins/ \
    && mkdir -p /opt/danser-grid \
    && cp /opt/danser/assets.dpak /opt/danser-grid/assets.dpak \
    && useradd --create-home --uid 1000 app \
    && mkdir -p /data/db /data/working /data/rendered /data/daily /data/logs /data/beatmaps/songs \
    && chown -R app:app /app /data /opt/danser /opt/danser-grid

# Grid renderer binary + its native libs (rpath-style lookup beside binary).
COPY --from=gridbuilder --chown=app:app /out/danser-grid /out/libbass.so \
    /out/libbass_fx.so /out/libbassmix.so /out/libyuv.so /opt/danser-grid/
ENV LD_LIBRARY_PATH=/opt/danser-grid:${LD_LIBRARY_PATH:-}

USER app

ENTRYPOINT ["scripts/entrypoint.sh"]
CMD ["discover"]
