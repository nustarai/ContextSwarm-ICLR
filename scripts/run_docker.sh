#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE="${CONTEXTSWARM_MINI_IMAGE:-contextswarm-iclr-mini:latest}"
AISW_BINARY="${CONTEXTSWARM_NUROUTER_BINARY:-${CONTEXTSWARM_AISW_BINARY:-${HOME}/.local/share/contextswarm/aisw-linux-aarch64}}"
NODE_CONFIG="${CONTEXTSWARM_NUROUTER_NODE_CONFIG:-${CONTEXTSWARM_AISW_NODE_CONFIG:-}}"
AISW_METADATA="${CONTEXTSWARM_AISW_LAUNCHER_METADATA:-}"
PI_HOME="${CONTEXTSWARM_PI_HOME:-}"
CODEX_HOME="${CONTEXTSWARM_CODEX_HOME:-}"
MEMORY="${CONTEXTSWARM_MINI_MEMORY:-16g}"
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
mkdir -p "${ROOT_DIR}/runs"

DOCKER_ARGS=(
  --rm
  --init
  --network host
  --memory "${MEMORY}"
  -v "${ROOT_DIR}:/opt/contextswarm:ro"
  -v "${ROOT_DIR}/runs:/opt/contextswarm/runs"
  -e "MINI_SWARM_NUROUTER_VERSION=${NUROUTER_VERSION}"
)

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
  if [[ -n "${PI_HOME}" ]]; then
    if [[ ! -d "${PI_HOME}" ]]; then
      echo "Pi home not found: ${PI_HOME}" >&2
      exit 2
    fi
    DOCKER_ARGS+=("-v" "${PI_HOME}:/root/.pi:ro")
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
