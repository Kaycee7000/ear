# syntax=docker/dockerfile:1
# Lightweight reverse-engineering & binary-analysis image for GitHub Actions (ghcr.io)
# Includes: radare2, r2ghidra (headless Ghidra decompiler via `pdg`), binutils, pytest, strace

FROM ubuntu:24.04

LABEL org.opencontainers.image.title="r2-ghidra-ci" \
      org.opencontainers.image.description="Lightweight CI container with radare2, r2ghidra, binutils, pytest, and strace" \
      org.opencontainers.image.source="https://github.com/Kaycee7000/ear" \
      org.opencontainers.image.licenses="LGPL-3.0-only"

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Pinned release versions & SHA-256 checksums (supports linux/amd64 and linux/arm64)
ARG R2VER=6.2.4
ARG R2_AMD64_SHA256=7019eedc0e0e87d1f53d6b8f5fc62b898567679c5efdce7fbfc557c9e7655e90
ARG R2_ARM64_SHA256=bbf48ee7bd5d8282f65a297dcabd34c9961a500e62d6b036679caab187b32ef1
ARG R2GHIDRA_AMD64_SHA256=e5c184cc6943f5e99bcb1ecc2f3bdf200e58865eea01edfbfb787b0e476ea385
ARG R2GHIDRA_ARM64_SHA256=a8ac843c4e23e6cbf3c4e0975d84215f4706172109130fa0ee2229bc56faec26

# Install runtime tools (binutils, strace, python3 + pytest) and pre-built radare2 + r2ghidra
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        binutils \
        ca-certificates \
        curl \
        python3 \
        python3-pytest \
        strace; \
    ARCH="$(dpkg --print-architecture)"; \
    case "$ARCH" in \
        amd64) \
            R2_SHA256="$R2_AMD64_SHA256"; \
            R2GHIDRA_SHA256="$R2GHIDRA_AMD64_SHA256"; \
            ;; \
        arm64) \
            R2_SHA256="$R2_ARM64_SHA256"; \
            R2GHIDRA_SHA256="$R2GHIDRA_ARM64_SHA256"; \
            ;; \
        *) \
            echo "Unsupported architecture: $ARCH" >&2; \
            exit 1; \
            ;; \
    esac; \
    curl -fsSL -o /tmp/radare2.deb \
        "https://github.com/radareorg/radare2/releases/download/${R2VER}/radare2_${R2VER}_${ARCH}.deb"; \
    echo "${R2_SHA256}  /tmp/radare2.deb" | sha256sum -c -; \
    apt-get install -y --no-install-recommends /tmp/radare2.deb; \
    if [ "$ARCH" = "amd64" ]; then \
        curl -fsSL -o /tmp/r2ghidra.deb \
            "https://github.com/radareorg/r2ghidra/releases/download/${R2VER}/r2ghidra_${R2VER}_amd64.deb"; \
        echo "${R2GHIDRA_SHA256}  /tmp/r2ghidra.deb" | sha256sum -c -; \
        apt-get install -y --no-install-recommends /tmp/r2ghidra.deb; \
    else \
        apt-get install -y --no-install-recommends unzip; \
        curl -fsSL -o /tmp/r2ghidra.zip \
            "https://github.com/radareorg/r2ghidra/releases/download/${R2VER}/r2ghidra-${R2VER}-linux-arm-64.zip"; \
        echo "${R2GHIDRA_SHA256}  /tmp/r2ghidra.zip" | sha256sum -c -; \
        unzip -q /tmp/r2ghidra.zip -d /tmp/r2ghidra; \
        PLUGDIR="$(r2 -H R2_LIBR_PLUGINS)"; \
        mkdir -p "$PLUGDIR"; \
        cp -a /tmp/r2ghidra/plugins/core_ghidra.so /tmp/r2ghidra/plugins/r2ghidra_sleigh "$PLUGDIR/"; \
        install -m 755 /tmp/r2ghidra/bin/sleighc /tmp/r2ghidra/bin/r2fidb /usr/local/bin/; \
        apt-get purge -y --auto-remove unzip; \
    fi; \
    apt-get purge -y --auto-remove curl; \
    rm -rf /tmp/radare2.deb /tmp/r2ghidra.deb /tmp/r2ghidra /tmp/r2ghidra.zip /var/lib/apt/lists/*; \
    # Verify all tools and headless Ghidra decompiler plugin (`pdg`) load cleanly \
    r2 -v; \
    r2 -qc "pdg?" - >/dev/null; \
    objdump --version | head -n 1; \
    pytest --version; \
    strace -V

WORKDIR /workspace
CMD ["/bin/bash"]
