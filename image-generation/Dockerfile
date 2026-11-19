# Prebuilt worker for image jobs: CPU PyTorch, diffusers and the rest of the pinned lock, installed once per
# change instead of on every run. Model weights are not baked in (they stay in the optional Actions cache).
FROM python:3.12-slim-bookworm@sha256:0f086103b026048cc50cb5da25ec435c023c5241a83fc44daea593377abc61c4
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_CACHE_DIR=1 HOME=/tmp
WORKDIR /app/image-generation
COPY worker/requirements.txt ./worker/requirements.txt
RUN python -m pip install --no-cache-dir -r worker/requirements.txt
COPY pyproject.toml ./
COPY ear_image ./ear_image
COPY worker ./worker
COPY tests ./tests
RUN python -m pip install --no-cache-dir . && python -m pip check
# Tells worker/run.py the lock is installed, so it skips its per-run pip install.
ENV EAR_IMAGE_PREBUILT=1
