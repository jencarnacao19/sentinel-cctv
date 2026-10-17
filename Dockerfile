#
# Sentinel CCTV runs as a single container: the API, the compiled web interface,
# the FFmpeg recorders and the motion workers all live in one process tree, with
# go2rtc supervised alongside them. One container means one thing to restart,
# one log stream and no inter-service networking for a home user to debug.

# --- Stage 1: build the web interface ----------------------------------------
# The compiled interface is plain static files, so this stage always runs on the
# build machine's own platform, even when the image is built for another one.
FROM --platform=$BUILDPLATFORM node:22-bookworm-slim@sha256:83f487e0a63425e5b4d146fb5e5be574bcbe1b7b843d3ebafdd95eaf7767a7e5 AS web

WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci --no-audit --no-fund

COPY frontend/ ./
RUN npm run build


# --- Stage 2: fetch go2rtc ---------------------------------------------------
FROM debian:bookworm-slim@sha256:88200866dfff7ea7f5cbcb6ec7c8a701889efe6fe859fe64d6990e4b07ea4171 AS go2rtc

# Bump deliberately: a stream proxy upgrade should be a reviewed change.
ARG GO2RTC_VERSION=v1.9.14
ARG TARGETARCH

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*

RUN set -eux; \
    case "${TARGETARCH}" in \
      amd64) asset="go2rtc_linux_amd64"; checksum="32d616af226bd731678ffde328b94cfb94e30339bfefc469cfb76323144615a6" ;; \
      arm64) asset="go2rtc_linux_arm64"; checksum="359fabade8a7a51e81a55fe6df6b0ef81764a5e1d63179577534eaaa71904b50" ;; \
      arm)   asset="go2rtc_linux_arm"; checksum="4d7e1639af5a2722a28e864468fd8099b3c1682565446c798bf9e3b38fde12e4" ;; \
      *) echo "Unsupported architecture: ${TARGETARCH}" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/go2rtc \
      "https://github.com/AlexxIT/go2rtc/releases/download/${GO2RTC_VERSION}/${asset}"; \
    echo "$checksum  /tmp/go2rtc" | sha256sum -c -; \
    install -m 0755 /tmp/go2rtc /usr/local/bin/go2rtc; \
    go2rtc --version


# --- Stage 3: runtime --------------------------------------------------------
FROM python:3.12-slim-bookworm@sha256:782412e85d0f0984994c290652577d4018aff08145c85b262bb63dc0c7522254 AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    SENTINEL_DATA_DIR=/data \
    SENTINEL_RECORDINGS_DIR=/recordings \
    SENTINEL_PORT=8090

# ffmpeg does the recording and the motion downscale; libgl/libglib are what
# OpenCV needs even in its headless build; tini reaps the FFmpeg children so a
# crashed recorder never becomes a zombie.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        tini \
        curl \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# App-only changes reuse this layer. The app install cannot resolve upgrades;
# transitive versions and artifact hashes are reviewed in Git.
COPY backend/requirements.lock backend/requirements-build.lock ./
RUN pip install --require-hashes -r requirements.lock -r requirements-build.lock

COPY backend/pyproject.toml ./
COPY backend/app ./app
RUN pip install --no-deps --no-build-isolation . && pip check

COPY backend/alembic.ini ./
COPY backend/alembic ./alembic
COPY --from=web /build/dist ./static
COPY --from=go2rtc /usr/local/bin/go2rtc /usr/local/bin/go2rtc

# Runs unprivileged. The volumes below must be writable by this uid; the
# Proxmox installer sets that up.
RUN groupadd --system --gid 1000 sentinel \
    && useradd --system --uid 1000 --gid sentinel --home /app sentinel \
    && mkdir -p /data /recordings \
    && chown -R sentinel:sentinel /app /data /recordings

USER sentinel

VOLUME ["/data", "/recordings"]
EXPOSE 8090/tcp 8555/tcp 8555/udp

# Reports unhealthy only when a critical component is down, which is what makes
# `docker restart` on failure the right response.
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8090/api/v1/system/health || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090"]
