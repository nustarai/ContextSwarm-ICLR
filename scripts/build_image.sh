#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${CONTEXTSWARM_MINI_IMAGE:-contextswarm-iclr-mini:latest}"
PI_VERSION="${CONTEXTSWARM_MINI_PI_VERSION:-0.84.2}"
CODEX_VERSION="${CONTEXTSWARM_MINI_CODEX_VERSION:-0.148.0}"
cd "${ROOT_DIR}"
exec docker build --build-arg "PI_VERSION=${PI_VERSION}" --build-arg "CODEX_VERSION=${CODEX_VERSION}" -t "${IMAGE}" .
