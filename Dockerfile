# Combined image: RH scanner + memelab collector + dashboard in ONE container.
#   docker build -t rh-suite .
#   docker run --rm -v "$PWD/data:/app/data" --env-file .env rh-suite
# One Railway service points here (Dockerfile path = repo root). Everything runs
# via run_all.py; toggle parts with RUN_SCANNER / RUN_MEMELAB / RUN_DASHBOARD.
FROM python:3.12-slim

WORKDIR /app

# Deps first for layer caching — both requirement sets, deduped by pip.
COPY rhl2_scanner/requirements.txt /app/req-scanner.txt
COPY memelab/requirements.txt /app/req-memelab.txt
RUN pip install --no-cache-dir -r /app/req-scanner.txt \
    && pip install --no-cache-dir -r /app/req-memelab.txt

# Packages + launcher.
COPY rhl2_scanner /app/rhl2_scanner
COPY memelab /app/memelab
COPY run_all.py /app/run_all.py

# Shared dataset volume (attach a Railway Volume here to persist across deploys).
RUN mkdir -p /app/data
ENV RHL2_DB_DIR=/app/data
ENV MEMELAB_DB=/app/data/memelab.db
ENV PYTHONUNBUFFERED=1

# Runs scanner + collector + dashboard together (each supervised).
CMD ["python", "/app/run_all.py"]
