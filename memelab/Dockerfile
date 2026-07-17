# memelab collector — multi-chain snapshot + backtest + screen loop.
# Build context = repo root (so it can COPY the memelab package).
#   docker build -t memelab -f memelab/Dockerfile .
FROM python:3.12-slim

WORKDIR /app

COPY memelab/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY memelab /app/memelab

# DB lives here — attach a Railway Volume mounted at /app/data to persist the
# dataset (the whole point). Store creates the dir on boot if missing.
RUN mkdir -p /app/data

# Runs the collector across all four chains, writing to the persistent volume.
# Set MEMELAB_TELEGRAM_TOKEN + MEMELAB_TELEGRAM_CHAT for alerts (else stdout).
ENTRYPOINT ["python", "-m", "memelab"]
CMD ["collect", "--db", "/app/data/memelab.db"]
