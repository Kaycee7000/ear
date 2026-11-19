# Toolbox for code checks: builds and tests run here with no credentials. Python 3.12, Node 26, uv, git and a
# C/C++ toolchain, matching the agent's workstation. The I/O helpers live in their own venv (/opt/ear), so the
# project's own Python environment starts clean.
FROM python:3.12-slim-bookworm@sha256:0f086103b026048cc50cb5da25ec435c023c5241a83fc44daea593377abc61c4
ENV PYTHONUNBUFFERED=1 PIP_DISABLE_PIP_VERSION_CHECK=1 NPM_CONFIG_UPDATE_NOTIFIER=false NPM_CONFIG_FUND=false \
    PATH=/opt/node/bin:$PATH
RUN apt-get update -qq \
 && apt-get install -y --no-install-recommends build-essential git curl ca-certificates xz-utils zip unzip pkg-config \
 && rm -rf /var/lib/apt/lists/*
ARG NODE_V=26.13.1
ARG NODE_SHA=5c063e46bab0fbc1c17cc445d7ff129cbdcd0471be6ed83b6ab864f8143bc864
RUN curl -fsSL -o /tmp/node.tar.xz "https://nodejs.org/dist/v${NODE_V}/node-v${NODE_V}-linux-x64.tar.xz" \
 && echo "${NODE_SHA}  /tmp/node.tar.xz" | sha256sum -c - \
 && mkdir /opt/node && tar -xJf /tmp/node.tar.xz -C /opt/node --strip-components=1 && rm /tmp/node.tar.xz \
 && ln -s /opt/node/bin/node /opt/node/bin/npm /opt/node/bin/npx /usr/local/bin/ \
 && node --version && npm --version
ARG UV_V=0.13.9
ARG UV_SHA=a7fbaff2b9a271d85c1bdb540941090fd1ce011797b4abeb48e4dcd39c2471d5
RUN curl -fsSL -o /tmp/uv.tar.gz "https://github.com/astral-sh/uv/releases/download/${UV_V}/uv-x86_64-unknown-linux-gnu.tar.gz" \
 && echo "${UV_SHA}  /tmp/uv.tar.gz" | sha256sum -c - \
 && tar -xzf /tmp/uv.tar.gz -C /tmp && install -m 755 /tmp/uv-x86_64-unknown-linux-gnu/uv /tmp/uv-x86_64-unknown-linux-gnu/uvx /usr/local/bin/ \
 && rm -rf /tmp/uv*
COPY worker/requirements.txt /app/requirements.txt
RUN python -m venv /opt/ear \
 && /opt/ear/bin/pip install --only-binary=:all: --no-cache-dir -r /app/requirements.txt \
 && /opt/ear/bin/pip check
COPY ear_code/plan.py /app/ear_code/plan.py
COPY worker/*.py /app/worker/
WORKDIR /app
USER 65532:65532
CMD ["bash"]
