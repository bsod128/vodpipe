# Everything the README claims is runnable from here.
# `make demo` is the one command a reviewer should need.

PY ?= python
BIN := .venv/bin
VENV := .venv
SRC ?= src_clip/4k_master.mp4

# On Windows set PY=.venv\Scripts\python and BIN=.venv\Scripts.
ifeq ($(OS),Windows_NT)
  BIN := .venv/Scripts
  ACTIVATE := $(BIN)\activate
else
  ACTIVATE := source $(BIN)/activate
endif

.PHONY: help venv install doctor master demo bench watermark figures test lint type all clean infra down

help:
	@echo "make install   - venv + package (no ffmpeg download)"
	@echo "make doctor    - what ffmpeg/GPU/encoders this machine actually has"
	@echo "make master    - generate the deterministic 4K source clip"
	@echo "make demo      - end-to-end ladder + HLS/DASH + VMAF, no Docker"
	@echo "make bench     - encoder/parallelism matrix (M2)"
	@echo "make watermark - watermark overhead matrix (M3)"
	@echo "make figures   - SVG charts from bench/*.json into docs/figures"
	@echo "make test      - pytest (GPU tests skip without VODPIPE_GPU_TESTS=1)"
	@echo "make lint      - ruff;  make type - mypy"
	@echo "make infra     - rabbitmq + redis in Docker (queue demo needs these)"
	@echo "make all       - install doctor master demo figures test"

venv:
	@[ -d $(VENV) ] || $(PY) -m venv $(VENV)

install: venv
	$(BIN)/python -m pip install --quiet --upgrade pip
	$(BIN)/python -m pip install --quiet -e ".[server,dev]"

doctor: install
	$(BIN)/python -m vodpipe doctor

master: install
	$(BIN)/python -m vodpipe master --src $(SRC)

demo: install
	$(BIN)/python -m vodpipe demo --src $(SRC)

bench: install
	$(BIN)/python -m vodpipe bench --src $(SRC)

watermark: install
	$(BIN)/python -m vodpipe watermark --src $(SRC)

figures: install
	$(BIN)/python -m vodpipe figures

test: install
	$(BIN)/python -m pytest -q

lint: install
	$(BIN)/python -m ruff check src tests

type: install
	$(BIN)/python -m mypy

# Queue demo: infrastructure only. The NVENC worker runs on the host because
# Docker Desktop here cannot pass a GPU into its WSL2 VM.
infra:
	docker compose up -d rabbitmq redis

down:
	docker compose stop rabbitmq redis

all: install doctor master demo figures test
