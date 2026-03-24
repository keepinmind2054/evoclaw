#!/bin/bash
# Build the EvoClaw agent container image

# BUG-19B-07 FIX: set -eo pipefail so that any failed command (including the
# docker build invocation) causes an immediate non-zero exit.  The previous
# `set -e` did not propagate failures through pipes.  We also explicitly
# verify that the image was registered in the local daemon after the build
# completes, because `docker build` can exit 0 in some edge cases (e.g.
# BuildKit cache hit without a successful new push) while the image tag does
# not exist.
set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

IMAGE_NAME="evoclaw-agent"
TAG="${1:-latest}"
CONTAINER_RUNTIME="${CONTAINER_RUNTIME:-docker}"

echo "Building EvoClaw agent container image..."
echo "Image: ${IMAGE_NAME}:${TAG}"

"${CONTAINER_RUNTIME}" build -t "${IMAGE_NAME}:${TAG}" .
BUILD_EXIT=$?
if [ "${BUILD_EXIT}" -ne 0 ]; then
    echo "ERROR: docker build failed (exit code ${BUILD_EXIT})" >&2
    exit 1
fi

# Verify the image actually landed in the local registry.
if ! "${CONTAINER_RUNTIME}" image inspect "${IMAGE_NAME}:${TAG}" > /dev/null 2>&1; then
    echo "ERROR: docker build exited 0 but image ${IMAGE_NAME}:${TAG} not found in local registry." >&2
    exit 1
fi

echo ""
echo "Build complete!"
echo "Image: ${IMAGE_NAME}:${TAG}"
echo ""
echo "Test with:"
echo "  echo '{\"prompt\":\"What is 2+2?\",\"groupFolder\":\"test\",\"chatJid\":\"test@g.us\",\"isMain\":false}' | \"${CONTAINER_RUNTIME}\" run -i ${IMAGE_NAME}:${TAG}"
