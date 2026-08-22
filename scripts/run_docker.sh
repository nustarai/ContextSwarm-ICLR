#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
AISW_BINARY="${CONTEXTSWARM_NUROUTER_BINARY:-${CONTEXTSWARM_AISW_BINARY:-${HOME}/.local/share/contextswarm/aisw-linux-aarch64}}"
NODE_CONFIG="${CONTEXTSWARM_NUROUTER_NODE_CONFIG:-${CONTEXTSWARM_AISW_NODE_CONFIG:-}}"
AISW_METADATA="${CONTEXTSWARM_AISW_LAUNCHER_METADATA:-}"
CODEX_HOME="${CONTEXTSWARM_CODEX_HOME:-}"
CONFIG="configs/cps.toml"
COMMAND="run"
MOCK=0
ARGS=()

if [[ ! -x "${AISW_BINARY}" ]] && command -v nurouter >/dev/null 2>&1; then
  AISW_BINARY="$(command -v nurouter)"
fi
if [[ ! -x "${AISW_BINARY}" ]] && command -v aisw >/dev/null 2>&1; then
  AISW_BINARY="$(command -v aisw)"
fi
if [[ -z "${NODE_CONFIG}" ]]; then
  if [[ -f "${HOME}/.nurouter/node.toml" ]]; then
    NODE_CONFIG="${HOME}/.nurouter/node.toml"
  else
    NODE_CONFIG="${HOME}/.aisw-codex/node.toml"
  fi
fi
if [[ -z "${AISW_METADATA}" ]]; then
  metadata_candidates=(
    "$(dirname "${AISW_BINARY}")/.aisw-pi-launcher.json"
    "$(dirname "${AISW_BINARY}")/.nurouter-pi-launcher.json"
  )
  if [[ "$(basename "${AISW_BINARY}")" == "nurouter" || "$(basename "${AISW_BINARY}")" == "pi" ]]; then
    metadata_candidates=(
      "$(dirname "${AISW_BINARY}")/.nurouter-pi-launcher.json"
      "$(dirname "${AISW_BINARY}")/.aisw-pi-launcher.json"
    )
  fi
  for candidate in "${metadata_candidates[@]}"
  do
    if [[ -f "${candidate}" ]]; then
      AISW_METADATA="${candidate}"
      break
    fi
  done
fi
NUROUTER_VERSION=""
if [[ -x "${AISW_BINARY}" ]]; then
  NUROUTER_VERSION="$(${AISW_BINARY} --version 2>/dev/null | sed -n '1p' | cut -c1-120 || true)"
fi

while (($#)); do
  case "$1" in
    --config)
      CONFIG="$2"
      shift 2
      ;;
    preflight|plan|validate)
      COMMAND="$1"
      shift
      ;;
    --mock-agent)
      MOCK=1
      ARGS+=("--mock-agent")
      shift
      ;;
    --mock-proved)
      ARGS+=("--mock-proved")
      shift
      ;;
    --dry-run)
      MOCK=1
      ARGS+=("--dry-run")
      shift
      ;;
    --output)
      ARGS+=("--output" "$2")
      shift 2
      ;;
    *)
      ARGS+=("$1")
      shift
      ;;
  esac
done

if [[ ! -f "${ROOT_DIR}/${CONFIG}" && ! -f "${CONFIG}" ]]; then
  echo "manifest not found: ${CONFIG}" >&2
  exit 2
fi

if ! RESOLVED_DOCKER_CONFIG="$(
  PYTHONPATH="${ROOT_DIR}${PYTHONPATH:+:${PYTHONPATH}}" \
    python3 - "${CONFIG}" "${ROOT_DIR}" <<'PY'
from pathlib import Path
import sys

from contextswarm_mini.config import ConfigError, load_config

try:
    config = load_config(sys.argv[1], Path(sys.argv[2]))
except (ConfigError, OSError, ValueError) as exc:
    print(f"docker manifest resolution failed: {exc}", file=sys.stderr)
    raise SystemExit(2)

print(config.docker_image)
print(config.docker_memory_mb)
print(config.docker_network)
PY
)"; then
  exit 2
fi
mapfile -t RESOLVED_DOCKER_VALUES <<<"${RESOLVED_DOCKER_CONFIG}"
if [[ "${#RESOLVED_DOCKER_VALUES[@]}" -ne 3 ]]; then
  echo "docker manifest resolution returned an invalid payload" >&2
  exit 2
fi

IMAGE="${CONTEXTSWARM_MINI_IMAGE:-${RESOLVED_DOCKER_VALUES[0]}}"
MEMORY="${CONTEXTSWARM_MINI_MEMORY:-${RESOLVED_DOCKER_VALUES[1]}m}"
NETWORK="${RESOLVED_DOCKER_VALUES[2]}"
if [[ "${#IMAGE}" -gt 512 || ! "${IMAGE}" =~ ^[A-Za-z0-9][A-Za-z0-9._/:@+-]*$ ]]; then
  echo "invalid Docker image from manifest or CONTEXTSWARM_MINI_IMAGE" >&2
  exit 2
fi
if [[ "${#MEMORY}" -gt 32 || ! "${MEMORY}" =~ ^[1-9][0-9]*([bBkKmMgG])?$ ]]; then
  echo "invalid Docker memory from manifest or CONTEXTSWARM_MINI_MEMORY" >&2
  exit 2
fi
if [[ "${NETWORK}" != "host" && "${NETWORK}" != "bridge" ]]; then
  echo "invalid Docker network from manifest" >&2
  exit 2
fi

mkdir -p "${ROOT_DIR}/runs"

DOCKER_ARGS=(
  --rm
  --init
  --memory "${MEMORY}"
  -v "${ROOT_DIR}:/opt/contextswarm:ro"
  -v "${ROOT_DIR}/runs:/opt/contextswarm/runs"
  -e "MINI_SWARM_NUROUTER_VERSION=${NUROUTER_VERSION}"
)

if [[ "${NETWORK}" == "bridge" ]]; then
  DOCKER_ARGS+=(
    --network bridge
    --add-host "host.docker.internal:host-gateway"
  )
else
  DOCKER_ARGS+=(--network host)
fi

if (( MOCK == 0 )); then
  if [[ ! -x "${AISW_BINARY}" ]]; then
    echo "NuRouter/AISW Linux ELF not found: ${AISW_BINARY}" >&2
    echo "Set CONTEXTSWARM_NUROUTER_BINARY (or CONTEXTSWARM_AISW_BINARY) or use --mock-agent for an offline smoke." >&2
    exit 2
  fi
  if [[ ! -f "${NODE_CONFIG}" ]]; then
    echo "NuRouter/AISW node config not found: ${NODE_CONFIG}" >&2
    exit 2
  fi
  DOCKER_ARGS+=(
    -v "${AISW_BINARY}:/opt/contextswarm-input/aisw/pi:ro"
    -v "${NODE_CONFIG}:/opt/contextswarm-input/aisw-private/node.toml:ro"
  )
  if [[ -n "${AISW_METADATA}" ]]; then
    DOCKER_ARGS+=("-v" "${AISW_METADATA}:/opt/contextswarm-input/aisw/$(basename "${AISW_METADATA}"):ro")
  fi
  if [[ -n "${CODEX_HOME}" ]]; then
    if [[ ! -d "${CODEX_HOME}" ]]; then
      echo "Codex home not found: ${CODEX_HOME}" >&2
      exit 2
    fi
    DOCKER_ARGS+=("-v" "${CODEX_HOME}:/root/.codex:ro")
  fi
fi

exec docker run "${DOCKER_ARGS[@]}" "${IMAGE}" \
  --config "${CONFIG}" "${COMMAND}" "${ARGS[@]}"
