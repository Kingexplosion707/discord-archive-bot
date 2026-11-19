FROM python:3.13-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libimage-exiftool-perl fonts-dejavu-core tzdata \
    && rm -rf /var/lib/apt/lists/*

# Cloudflare's tunnel program, for the public address (Stremio addons and the lyrics page).
ARG CLOUDFLARED_VERSION=2026.10.0
RUN python -c "import platform, urllib.request; arch = {'x86_64': 'amd64', 'aarch64': 'arm64'}[platform.machine()]; urllib.request.urlretrieve(f'https://github.com/cloudflare/cloudflared/releases/download/${CLOUDFLARED_VERSION}/cloudflared-linux-{arch}', '/usr/local/bin/cloudflared')" \
    && chmod 755 /usr/local/bin/cloudflared \
    && cloudflared --version

# Node.js, for TV Tracker (the movie and TV bot that runs beside this one).
ARG NODE_VERSION=22.12.0
COPY docker/install_node.py /tmp/install_node.py
RUN python /tmp/install_node.py ${NODE_VERSION} && rm /tmp/install_node.py \
    && node --version && npm --version

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY tvtracker/package.json tvtracker/package-lock.json ./tvtracker/
RUN cd tvtracker && npm ci --omit=dev --no-audit --no-fund && npm cache clean --force

COPY tvtracker ./tvtracker
COPY bot ./bot

RUN useradd --uid 10001 --create-home archive \
    && mkdir -p /data /secrets \
    && chown archive:archive /data
USER archive

ENV PYTHONUNBUFFERED=1 \
    DATA_DIR=/data

ENTRYPOINT ["python", "-m", "bot"]
CMD ["run"]
