FROM python:3.13-slim-bookworm

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg libimage-exiftool-perl fonts-dejavu-core tzdata \
    && rm -rf /var/lib/apt/lists/*

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
