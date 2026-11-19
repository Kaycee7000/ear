# Prebuilt renderer for document jobs: LibreOffice Writer, fonts and the pinned Python renderers, so a job
# pulls one image instead of installing them on every run.
FROM python:3.12-slim-bookworm@sha256:0f086103b026048cc50cb5da25ec435c023c5241a83fc44daea593377abc61c4
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 HOME=/tmp
RUN apt-get update -qq \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      libreoffice-writer fonts-dejavu-core fonts-noto-core fonts-noto-cjk \
 && rm -rf /var/lib/apt/lists/*
WORKDIR /app/document-generation
COPY pyproject.toml ./
COPY ear_document ./ear_document
COPY worker ./worker
COPY tests ./tests
COPY examples ./examples
RUN python -m pip install --no-cache-dir . -r worker/requirements.txt && python -m pip check
USER 65532:65532
CMD ["python", "worker/run.py"]
