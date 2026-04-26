#!/usr/bin/env bash
# ============================================================
# build_push_docker.sh
# ------------------------------------------------------------
# Build + push image pe DockerHub (user: sdancri)
# Multi-arch (linux/amd64 + linux/arm64) daca ai buildx.
# Usage:
#   ./scripts/build_push_docker.sh [tag]
# Default tag: latest
# ============================================================
set -euo pipefail

DOCKERHUB_USER="sdancri"
IMAGE_NAME="trading-bot-boilerplate"
TAG="${1:-latest}"
FULL_IMAGE="${DOCKERHUB_USER}/${IMAGE_NAME}:${TAG}"

cd "$(dirname "$0")/.."

# --- Login (daca nu esti deja logat) ---
if ! docker info 2>/dev/null | grep -q "Username: ${DOCKERHUB_USER}"; then
    echo ">> Nu esti logat ca ${DOCKERHUB_USER}. Ruleaza: docker login"
    docker login -u "${DOCKERHUB_USER}"
fi

# --- Build + push ---
if docker buildx version &>/dev/null; then
    echo ">> Multi-arch build (amd64 + arm64) -> ${FULL_IMAGE}"
    docker buildx build \
        --platform linux/amd64,linux/arm64 \
        -t "${FULL_IMAGE}" \
        --push \
        .
else
    echo ">> Single-arch build -> ${FULL_IMAGE}"
    docker build -t "${FULL_IMAGE}" .
    docker push "${FULL_IMAGE}"
fi

echo ""
echo "✅ DONE — pull cu:"
echo "   docker pull ${FULL_IMAGE}"
echo ""
