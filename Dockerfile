#
# Multi-stage build for the cyber inventory service.
#
# Two stages, and the reason matters: the "builder" stage has compilers and
# package tooling in it, and none of that should reach production. Every tool
# left in a shipped image is extra attack surface and extra CVEs for a scanner
# to flag. So we build dependencies in one stage and copy only the finished
# result into a clean runtime stage.

# --------------------------------------------------------------------------
# Stage 1: builder
# --------------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS builder

# A virtualenv inside the image feels redundant -- the container is already
# isolated. It is here because it puts every installed dependency under one
# directory, which makes the copy into the runtime stage a single clean
# instruction instead of hunting through system site-packages.
ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN python -m venv "$VIRTUAL_ENV"

WORKDIR /build

# ORDER IS DELIBERATE AND IS THE SINGLE BIGGEST BUILD-SPEED LEVER.
#
# Docker caches each instruction as a layer and reuses it when the inputs are
# unchanged. Requirements are copied and installed BEFORE the application code
# is copied, so editing a Python file does not invalidate the dependency
# install. Copy the code first and every one-character change re-downloads and
# reinstalls every package -- turning a 5-second build into a 2-minute one, on
# every commit, for the life of the project.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# --------------------------------------------------------------------------
# Stage 2: runtime
# --------------------------------------------------------------------------
FROM python:3.11-slim-bookworm AS runtime

# Build args carrying provenance. These are supplied by CI:
#   docker build --build-arg GIT_COMMIT=$GITHUB_SHA ...
# They are promoted to ENV below so the running process can read them.
ARG GIT_COMMIT=unknown
ARG BUILD_TIME=unknown
ARG APP_VERSION=0.1.0

# PYTHONDONTWRITEBYTECODE: skip .pyc files. They bloat the image and are
#   never reused, since the container filesystem is thrown away each run.
# PYTHONUNBUFFERED: send stdout straight out instead of buffering it. Without
#   this, logs arrive late or vanish entirely when a container is killed --
#   which is exactly the moment you need them.
#
# (Comments live above this block rather than inside it. Docker does allow
# comment lines between backslash continuations, but it reads badly and some
# older tooling mishandles it. Not worth the cleverness.)
ENV GIT_COMMIT=${GIT_COMMIT} \
    BUILD_TIME=${BUILD_TIME} \
    APP_VERSION=${APP_VERSION} \
    SERVICE_NAME=cyber-inventory \
    VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# OCI labels. Standard metadata that `docker inspect` and most registries and
# scanners read. This is provenance that survives even if the container will
# not start, so you can identify a broken image without running it.
LABEL org.opencontainers.image.title="cyber-inventory" \
      org.opencontainers.image.description="Asset inventory service" \
      org.opencontainers.image.revision="${GIT_COMMIT}" \
      org.opencontainers.image.created="${BUILD_TIME}" \
      org.opencontainers.image.version="${APP_VERSION}" \
      org.opencontainers.image.source="https://github.com/mangohero8/cyber-inventory"

# Run as a non-root user.
#
# By default a container runs as root. If someone finds a way to execute code
# in your process, root inside the container is a materially better starting
# position for escaping to the host than an unprivileged account. Every
# container security scanner flags root, and it is one of the easiest findings
# to fix -- three lines.
RUN groupadd --system --gid 1001 appuser \
    && useradd --system --uid 1001 --gid appuser --no-create-home appuser

COPY --from=builder /opt/venv /opt/venv

WORKDIR /app
# --chown means the files are owned by appuser on arrival. Copying as root and
# then running `chown -R` afterwards duplicates the entire directory into a
# second image layer, doubling its contribution to image size.
COPY --chown=appuser:appuser app/ ./app/

USER appuser

# Cloud Run injects a PORT environment variable and expects the container to
# listen on it. Hardcoding 8080 works today only because that is the default
# Cloud Run happens to use -- if it ever sends a different port, a hardcoded
# container silently fails its health check with no useful error. Read $PORT.
ENV PORT=8080
EXPOSE 8080

# Exec form (JSON array), not shell form. In shell form the process runs as a
# child of /bin/sh, which does not forward SIGTERM -- so the container ignores
# shutdown signals and gets force-killed after the grace period, dropping any
# in-flight requests. Here sh is used deliberately to expand $PORT, with exec
# to replace the shell so uvicorn becomes PID 1 and receives signals directly.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT}"]
