# RH L2 memecoin scanner — minimal runtime image.
#   docker build -t rhl2-scanner -f rhl2_scanner/Dockerfile .
#   docker run --rm -v "$PWD/rhl2_scanner/config:/app/config" \
#     --env-file rhl2_scanner/.env rhl2-scanner paper
FROM python:3.12-slim

WORKDIR /app

# Deps first for layer caching.
COPY rhl2_scanner/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Package.
COPY rhl2_scanner /app/rhl2_scanner

# scanner.db persists here; mount a volume to keep it across restarts.
VOLUME ["/app/data"]
ENV RHL2_DB_DIR=/app/data

# Default: paper-mode calibration against config/config.yaml.
ENTRYPOINT ["python", "-m", "rhl2_scanner"]
CMD ["paper", "--config", "config/config.yaml"]
