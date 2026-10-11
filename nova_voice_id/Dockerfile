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

# Models are downloaded here, at build time, and checked against the SHA-256
# in models.txt. A mismatch fails the build. Nothing is downloaded at runtime.
COPY nova_voice_id/models.txt /usr/src/models/models.txt
RUN \
    set -eu; \
    cd /usr/src/models; \
    grep -v '^#' models.txt | while read -r sha file url; do \
        [ -n "${sha}" ] || continue; \
        curl -fsSL --retry 3 -o "${file}" "${url}"; \
        echo "${sha}  ${file}" | sha256sum -c -; \
    done; \
    chmod 0644 ./*.onnx

COPY nova_voice_id/MODEL_LICENCES /usr/src/models/MODEL_LICENCES
COPY src/nova_voice_id /usr/src/app/nova_voice_id

ENV \
    PATH="/usr/src/.venv/bin:${PATH}" \
    PYTHONPATH="/usr/src/app" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /
COPY nova_voice_id/rootfs /

HEALTHCHECK --start-period=60s --start-interval=5s --interval=30s \
    CMD ["python3", "-m", "nova_voice_id.health"]
