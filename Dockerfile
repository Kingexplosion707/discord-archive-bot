FROM python:3.13-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libimage-exiftool-perl fonts-dejavu-core tzdata \
    && rm -rf /var/lib/apt/lists/*

# Cloudflare's tunnel program, for the public address (Stremio addons and the lyrics page).
ARG CLOUDFLARED_VERSION=2026.10.0
RUN python -c "import platform, urllib.request; arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}[platform.machine()]; urllib.request.urlretrieve(f'https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}/cloudflared-linux-{arch}', '/usr/local/bin/cloudflared')" \
    && chmod 755 /usr/local/bin/cloudflared \
    && cloudflared --version

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot

RUN useradd --uid 10001 --create-home archive \
    && mkdir -p /data /secrets \
    && chown archive:archive /data
USER archive

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

ENTRYPOINT ["python", "-m", "bot"]
CMD ["run"]
