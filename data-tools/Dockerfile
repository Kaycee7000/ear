FROM python:3.12-slim-bookworm@sha256:0f086103b026048cc50cb5da25ec435c023c5241a83fc44daea593377abc61c4
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 HOME=/tmp MPLCONFIGDIR=/tmp/mpl MPLBACKEND=Agg OPENBLAS_NUM_THREADS=1
WORKDIR /app
COPY worker/requirements.txt /app/requirements.txt
RUN python -m pip install --only-binary=:all: --no-cache-dir --disable-pip-version-check -r /app/requirements.txt && python -m pip check
COPY ear_data/plan.py /app/ear_data/plan.py
COPY worker/*.py /app/worker/
USER 65532:65532
CMD ["python", "-m", "worker.compute"]
