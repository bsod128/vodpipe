# -*- coding: utf-8 -*-
"""Конфигурация через окружение: контейнер и хост-воркер смотрят в одну точку."""
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # .../media

MEDIA_ROOT = os.environ.get("VODPIPE_MEDIA", ROOT)
WORK_ROOT = os.environ.get("VODPIPE_WORK", os.path.join(MEDIA_ROOT, "work", "jobs"))
OUT_ROOT = os.environ.get("VODPIPE_OUT", os.path.join(MEDIA_ROOT, "out", "jobs"))

BROKER_URL = os.environ.get("VODPIPE_BROKER", "amqp://guest:guest@localhost:5672/")
RESULT_BACKEND = os.environ.get("VODPIPE_BACKEND", "redis://localhost:6379/0")
STATE_URL = os.environ.get("VODPIPE_STATE", RESULT_BACKEND)

DEFAULT_SOURCE = os.environ.get("VODPIPE_SRC", os.path.join(MEDIA_ROOT, "src_clip", "4k_master.mp4"))
GPU_AUTO = os.environ.get("VODPIPE_GPU_AUTO", "1") != "0"
