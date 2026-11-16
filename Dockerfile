# syntax=docker/dockerfile:1
#
# Bamboo MCP Services: all agents under bamboo-supervisor (decisions S-12 – S-24).
#
# Build with docker/build.sh, which fixes the platform (linux/amd64, also from
# an Apple Silicon Mac), the version and the commit.  README-container.md has
# the full procedure, the aipanda033 run command and the verification steps.
#
# The build needs docker/model-manifest.sha256, generated on aipanda033 from
# the model directory that built the vector store.  Without it the build stops
# at the COPY below: there is deliberately no fallback.

ARG PYTHON_IMAGE=python:3.12-slim-bookworm
# The model snapshot that built the vector store on aipanda033 (S-15).
ARG MODEL_REPO=sentence-transformers/all-MiniLM-L6-v2
ARG MODEL_REVISION=1110a243fdf4706b3f48f1d95db1a4f5529b4d41
# Set by docker/build.sh.
ARG VERSION=unknown
ARG GIT_COMMIT=unknown

# ──────────────────────────────────────────────────────────────────────────────
FROM ${PYTHON_IMAGE} AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN python -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
WORKDIR /build

# 1. Dependencies, exactly as locked (S-21).  torch first, as the CPU build of
#    the locked version from PyTorch's CPU index (S-14); everything else from
#    PyPI.  --no-deps everywhere, so a package missing from the lock fails
#    'pip check' below instead of being resolved to whatever is newest.
COPY requirements/runtime-py312.txt requirements/
RUN set -eux; \
    torch_version="$(sed -n 's/^torch==//p' requirements/runtime-py312.txt)"; \
    test -n "${torch_version}"; \
    pip install --no-deps --index-url https://download.pytorch.org/whl/cpu "torch==${torch_version}"; \
    grep -v '^torch==' requirements/runtime-py312.txt > /tmp/runtime-without-torch.txt; \
    pip install --no-deps -r /tmp/runtime-without-torch.txt

# 2. The project itself, installed from its wheel (S-22).
COPY pyproject.toml LICENSE README.md ./
COPY src ./src
RUN set -eux; \
    pip wheel --no-deps --wheel-dir /tmp/dist .; \
    pip install --no-deps /tmp/dist/*.whl; \
    pip check

# 3. The embedding model, bit-identical to the snapshot that built the store
#    (S-15): the manifest's files at a pinned commit, verified by SHA-256.
ARG MODEL_REPO
ARG MODEL_REVISION
COPY scripts/fetch_model.py docker/model-manifest.sha256 /build/model/
RUN python /build/model/fetch_model.py \
        --repo "${MODEL_REPO}" \
        --revision "${MODEL_REVISION}" \
        --manifest /build/model/model-manifest.sha256 \
        --dest /opt/bamboo/models/sentence-transformers/all-MiniLM-L6-v2

# 4. What went into this image, for anyone holding only the image.
ARG GIT_COMMIT
RUN set -eux; \
    lock_sha256="$(sha256sum requirements/runtime-py312.txt | cut -d' ' -f1)"; \
    manifest_sha256="$(sha256sum /build/model/model-manifest.sha256 | cut -d' ' -f1)"; \
    version="$(python -c 'import importlib.metadata as m; print(m.version("bamboo-mcp-services"))')"; \
    torch_build="$(python -c 'import torch; print(torch.__version__)')"; \
    printf '{\n  "version": "%s",\n  "git_commit": "%s",\n  "lock_sha256": "%s",\n  "torch": "%s",\n  "model_repo": "%s",\n  "model_revision": "%s",\n  "model_manifest_sha256": "%s"\n}\n' \
        "${version}" "${GIT_COMMIT}" "${lock_sha256}" "${torch_build}" \
        "${MODEL_REPO}" "${MODEL_REVISION}" "${manifest_sha256}" \
        > /opt/bamboo/build-info.json; \
    cat /opt/bamboo/build-info.json

# ──────────────────────────────────────────────────────────────────────────────
FROM ${PYTHON_IMAGE} AS runtime

# git: github-sync clones repositories.  tini: PID 1 / subreaper (S-14).
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends git tini ca-certificates; \
    rm -rf /var/lib/apt/lists/*

# Runs under any UID (S-17): the default user is 10001, but everything it
# must write is owned by group 0 with group permissions equal to the owner's,
# the convention for platforms that assign an arbitrary UID in group 0.  On
# aipanda033 the container runs as atlpan with --group-add 0.
RUN set -eux; \
    useradd --uid 10001 --gid 0 --no-log-init --create-home --home-dir /home/bamboo \
        --shell /usr/sbin/nologin bamboo; \
    mkdir -p /data/bamboo/supervisor/logs /tmp/bamboo; \
    chown -R 10001:0 /home/bamboo /data/bamboo /tmp/bamboo; \
    chmod -R g=u /home/bamboo /data/bamboo /tmp/bamboo

COPY --from=builder /opt/venv /opt/venv
COPY --from=builder /opt/bamboo /opt/bamboo
COPY scripts/check_embedding_model.py /opt/bamboo/scripts/

# HF_HUB_OFFLINE / TRANSFORMERS_OFFLINE: nothing may reach the Hub at run time
# (S-15).  ANONYMIZED_TELEMETRY=False: ChromaDB's telemetry is on by default.
# OMP_NUM_THREADS is deliberately unset: torch then uses every core it sees,
# as on aipanda033.  Under a CPU limit (Kubernetes) set it to that limit.
ENV PATH=/opt/venv/bin:$PATH \
    HOME=/home/bamboo \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    ANONYMIZED_TELEMETRY=False \
    TOKENIZERS_PARALLELISM=false

USER 10001

# Build-time smoke test, as the runtime user: the model loads offline as
# Transformer -> Pooling -> Normalize with unit-norm 384-dim output; torch is
# the CPU build; the entry points are on PATH.
RUN set -eux; \
    python /opt/bamboo/scripts/check_embedding_model.py \
        /opt/bamboo/models/sentence-transformers/all-MiniLM-L6-v2; \
    python -c 'import torch, duckdb, chromadb; assert torch.version.cuda is None, torch.version.cuda'; \
    for cmd in bamboo-supervisor bamboo-cric bamboo-ingestion bamboo-github-sync \
               bamboo-document-monitor bamboo-core-reaper; do \
        command -v "${cmd}"; \
    done; \
    git --version

# Same paths as aipanda033 (S-12, S-13): the reference configuration and the
# agent configs under /data/bamboo are used unchanged.
WORKDIR /data/bamboo/supervisor

ARG VERSION
ARG GIT_COMMIT
ARG MODEL_REVISION
LABEL org.opencontainers.image.title="bamboo-mcp-services" \
      org.opencontainers.image.description="Bamboo MCP Services agents under bamboo-supervisor" \
      org.opencontainers.image.source="https://github.com/PalNilsson/bamboo-mcp-services" \
      org.opencontainers.image.licenses="Apache-2.0" \
      org.opencontainers.image.version="${VERSION}" \
      org.opencontainers.image.revision="${GIT_COMMIT}" \
      ch.cern.bamboo.model-revision="${MODEL_REVISION}"

# Docker marks the container unhealthy when the supervisor loop stops writing
# its heartbeat (S-18).  The path must match --heartbeat-file in CMD.
HEALTHCHECK --interval=60s --timeout=15s --start-period=60s --retries=3 \
    CMD ["bamboo-supervisor", "--status", "--heartbeat-file", "/data/bamboo/supervisor/heartbeat.json", "--max-age", "120"]

# The supervisor stops its children within stop_timeout_s (30 s): run with
# 'docker run --stop-timeout 45', and terminationGracePeriodSeconds >= 45 in
# Kubernetes.  tini -s also works when the container shares the host's PID
# namespace (--pid=host, S-23) and tini is not PID 1.
ENTRYPOINT ["tini", "-s", "--", "bamboo-supervisor"]
CMD ["--config", "/data/bamboo/supervisor/supervisor-agent.yaml", \
     "--log-file", "/data/bamboo/supervisor/logs/supervisor-agent.log", \
     "--heartbeat-file", "/data/bamboo/supervisor/heartbeat.json"]
