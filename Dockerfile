# API-контейнер: только HTTP + постановка в очередь, ffmpeg ему не нужен
# (кодит воркер). Отдельный образ воркера с ffmpeg сознательно не собран:
# на этой машине Docker Desktop не пробрасывает GPU (cuInit -> CUDA_ERROR_NO_DEVICE),
# поэтому NVENC-воркер живёт на хосте. На Linux-сервере этот же образ покрывает
# и воркер:  docker compose run worker celery -A tasks worker -l info
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app

COPY api/requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY api /app
COPY vodpipe /app/vodpipe

ENV VODPIPE_MEDIA=/media
EXPOSE 8000
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
