# Build from the repo root:
#   docker build -f nova_voice_id/Dockerfile --build-arg BUILD_FROM=... .
ARG BUILD_FROM
FROM ${BUILD_FROM}

WORKDIR /usr/src

COPY requirements.txt /usr/src/requirements.txt

RUN \
    apt-get update \
    && apt-get install -y --no-install-recommends \
        python3 \
        python3-venv \
    && python3 -m venv /usr/src/.venv \
    && /usr/src/.venv/bin/pip install --no-cache-dir --require-hashes \
        -r /usr/src/requirements.txt \
    && useradd --system --no-create-home --shell /usr/sbin/nologin nova \
    && rm -rf /var/lib/apt/lists/*

COPY src/nova_voice_id /usr/src/app/nova_voice_id

ENV \
    PATH="/usr/src/.venv/bin:${PATH}" \
    PYTHONPATH="/usr/src/app" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /
COPY nova_voice_id/rootfs /

HEALTHCHECK --start-period=30s --start-interval=5s --interval=30s \
    CMD ["python3", "-m", "nova_voice_id.health"]
