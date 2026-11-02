# API container: HTTP + queueing only. It never encodes, so it does not use
# ffmpeg - but the same image also covers the `gpu` worker profile, which does,
# hence the ffmpeg install below.
#
# A separate GPU worker image is deliberately not built here: on this machine
# Docker Desktop cannot pass a GPU into its WSL2 VM (cuInit returns
# CUDA_ERROR_NO_DEVICE), so the NVENC worker runs on the host. On a Linux
# server the same image covers the worker:
#   docker compose --profile gpu up worker
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

# ffmpeg/ffprobe for the worker profile; the API service ignores them.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# Install the package before copying the rest so the layer survives source edits
# that do not touch dependencies.
COPY pyproject.toml /app/pyproject.toml
COPY src /app/src
RUN pip install --no-cache-dir "/app[server]"

ENV VODPIPE_MEDIA=/media
EXPOSE 8000
CMD ["uvicorn", "vodpipe.server.main:app", "--host", "0.0.0.0", "--port", "8000"]
