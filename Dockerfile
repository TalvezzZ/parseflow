# syntax=docker/dockerfile:1

# Build the React workbench first so the final image contains both frontend and backend.
FROM node:22-bookworm-slim AS web-build
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web ./
RUN npm run build


# One production image: Nginx serves the workbench and proxies to a local,
# single-worker FastAPI process. LibreOffice, FFmpeg, and CPU PaddleOCR are
# included so every advertised local parsing capability is available.
FROM python:3.12-slim-bookworm AS full

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    HOME=/home/parseflow \
    FILE_STORAGE_DIR=/data/files \
    OFFICE_CONVERTER_COMMAND=soffice \
    MEDIA_FFMPEG_COMMAND=ffmpeg \
    MEDIA_FFPROBE_COMMAND=ffprobe

COPY --from=ghcr.io/astral-sh/uv:0.6.16 /uv /uvx /bin/

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        gettext-base \
        libreoffice-core \
        libreoffice-writer \
        libreoffice-calc \
        libreoffice-impress \
        fonts-noto-cjk \
        libgomp1 \
        libglib2.0-0 \
        nginx-light \
        tini \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --system parseflow \
    && useradd --system --create-home --gid parseflow --home-dir /home/parseflow parseflow \
    && mkdir -p /app /data/files /home/parseflow/.paddlex \
    && chown -R parseflow:parseflow /app /data /home/parseflow

WORKDIR /app

# Keep dependency installation cached until dependency metadata changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --group ocr --no-install-project

COPY app ./app
COPY README.md ./README.md
RUN uv sync --frozen --no-dev --group ocr \
    && chown -R parseflow:parseflow /app

COPY deploy/nginx.conf.template /etc/parseflow/nginx.conf.template
COPY deploy/docker-entrypoint.sh /usr/local/bin/parseflow-entrypoint
COPY --from=web-build /web/dist /usr/share/nginx/html
RUN chmod 0755 /usr/local/bin/parseflow-entrypoint \
    && rm -f /etc/nginx/sites-enabled/default

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=45s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1/health', timeout=3)"

ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/parseflow-entrypoint"]
