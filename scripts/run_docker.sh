#!/usr/bin/env bash
# This launcher receives operator-private capabilities through its environment.
# Keep this as the first executable command: even when a caller uses `bash -x`
# or redirects tracing with BASH_XTRACEFD, no later variable expansion may be
# written to a trace.  The command itself expands no environment values.
{ set +x; } 2>/dev/null
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
AISW_BINARY="${CONTEXTSWARM_NUROUTER_BINARY:-${CONTEXTSWARM_AISW_BINARY:-${HOME}/.local/share/contextswarm/aisw-linux-aarch64}}"
NODE_CONFIG="${CONTEXTSWARM_NUROUTER_NODE_CONFIG:-${CONTEXTSWARM_AISW_NODE_CONFIG:-}}"
AISW_METADATA="${CONTEXTSWARM_AISW_LAUNCHER_METADATA:-}"
CODEX_HOME="${CONTEXTSWARM_CODEX_HOME:-}"
PIDS_LIMIT="${CONTEXTSWARM_MINI_PIDS_LIMIT:-768}"
RUNTIME_TMPFS_SIZE="${CONTEXTSWARM_MINI_RUNTIME_TMPFS_SIZE:-1g}"
TMP_TMPFS_SIZE="${CONTEXTSWARM_MINI_TMP_TMPFS_SIZE:-2g}"
RUN_UID="${CONTEXTSWARM_MINI_RUN_UID:-$(id -u)}"
RUN_GID="${CONTEXTSWARM_MINI_RUN_GID:-$(id -g)}"
CONFIG="configs/cps.toml"
COMMAND="run"
MOCK=0
ARGS=()

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
if [[ -f "${ROOT_DIR}/${CONFIG}" ]]; then
  CONFIG_PATH="${ROOT_DIR}/${CONFIG}"
else
  CONFIG_PATH="${CONFIG}"
fi
FORMAL_LAUNCH=0
if (( MOCK == 0 )) && [[ "${COMMAND}" == "run" || "${COMMAND}" == "preflight" ]]; then
  FORMAL_LAUNCH=1
fi
if ! RESOLVED_RUNTIME_CONFIG="$(
  PYTHONPATH="${ROOT_DIR}${PYTHONPATH:+:${PYTHONPATH}}" \
    python3 - "${CONFIG_PATH}" "${ROOT_DIR}" "${FORMAL_LAUNCH}" <<'PY'
from pathlib import Path
import sys

from contextswarm_mini.launch_contract import (
    LaunchContractError,
    resolve_launch_contract,
)

try:
    contract = resolve_launch_contract(
        Path(sys.argv[1]),
        Path(sys.argv[2]),
        formal=sys.argv[3] == "1",
    )
    config = contract.config
except (LaunchContractError, OSError, ValueError) as exc:
    print(f"runtime manifest resolution failed: {exc}", file=sys.stderr)
    raise SystemExit(2)

print(config.docker_image)
print(config.docker_memory_mb)
print("1" if config.lean_require_result_cache_disabled else "0")
print(contract.container_manifest)
print(contract.manifest_sha256)
PY
)"; then
  exit 2
fi
mapfile -t RESOLVED_RUNTIME_VALUES <<<"${RESOLVED_RUNTIME_CONFIG}"
if [[ "${#RESOLVED_RUNTIME_VALUES[@]}" -ne 5 ]]; then
  echo "runtime manifest resolution returned an invalid payload" >&2
  exit 2
fi

IMAGE="${CONTEXTSWARM_MINI_IMAGE:-${RESOLVED_RUNTIME_VALUES[0]}}"
MEMORY="${CONTEXTSWARM_MINI_MEMORY:-${RESOLVED_RUNTIME_VALUES[1]}m}"
CACHE_DISABLED_REQUIRED="${RESOLVED_RUNTIME_VALUES[2]}"
CONFIG="${RESOLVED_RUNTIME_VALUES[3]}"
MANIFEST_SHA256="${RESOLVED_RUNTIME_VALUES[4]}"
if [[ "${#IMAGE}" -gt 512 || ! "${IMAGE}" =~ ^[A-Za-z0-9][A-Za-z0-9._/:@+-]*$ ]]; then
  echo "invalid Docker image from manifest or CONTEXTSWARM_MINI_IMAGE" >&2
  exit 2
fi
if [[ "${#MEMORY}" -gt 32 || ! "${MEMORY}" =~ ^[1-9][0-9]*([bBkKmMgG])?$ ]]; then
  echo "invalid Docker memory from manifest or CONTEXTSWARM_MINI_MEMORY" >&2
  exit 2
fi

for numeric_value in "${RUN_UID}" "${RUN_GID}" "${PIDS_LIMIT}"
do
  case "${numeric_value}" in
    ""|*[!0-9]*)
      echo "container UID, GID, and PID limit must be positive integers" >&2
      exit 2
      ;;
  esac
done
if [[ "${RUN_UID}" =~ ^0+$ || "${RUN_GID}" =~ ^0+$ ]]; then
  echo "refusing to launch the experiment container as root; run as a regular host user" >&2
  exit 2
fi
if (( PIDS_LIMIT < 1 )); then
  echo "container PID limit must be a positive integer" >&2
  exit 2
fi

NEEDS_JUDGE="${FORMAL_LAUNCH}"
if (( NEEDS_JUDGE == 1 )) && [[ -z "${CONTEXTSWARM_JUDGE_URL:-}" ]]; then
  echo "CONTEXTSWARM_JUDGE_URL must be set for a real run or preflight" >&2
  exit 2
fi
if [[ -n "${CONTEXTSWARM_JUDGE_URL:-}" ]]; then
  case "${CONTEXTSWARM_JUDGE_URL}" in
    http://*|https://*) ;;
    *)
      echo "CONTEXTSWARM_JUDGE_URL must use http:// or https://" >&2
      exit 2
      ;;
  esac
fi
if (( NEEDS_JUDGE == 1 )) && [[ "${CACHE_DISABLED_REQUIRED}" == "1" ]] && [[ -z "${CONTEXTSWARM_JUDGE_CACHE_HEALTH_URL:-}" ]]; then
  echo "CONTEXTSWARM_JUDGE_CACHE_HEALTH_URL must be set when disabled Judge result cache is required" >&2
  exit 2
fi
if [[ -n "${CONTEXTSWARM_JUDGE_CACHE_HEALTH_URL:-}" ]]; then
  case "${CONTEXTSWARM_JUDGE_CACHE_HEALTH_URL}" in
    http://*|https://*) ;;
    *)
      echo "CONTEXTSWARM_JUDGE_CACHE_HEALTH_URL must use http:// or https://" >&2
      exit 2
      ;;
  esac
fi

IMAGE_ID="$(docker image inspect --format '{{.Id}}' "${IMAGE}" 2>/dev/null || true)"
if [[ ! "${IMAGE_ID}" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "experiment image is missing a canonical image ID: ${IMAGE}" >&2
  exit 2
fi
# Resolve the mutable operator-facing tag exactly once.  All subsequent
# inspection and execution use that immutable local image ID so a concurrent
# tag update cannot separate the recorded provenance from the running bytes.
IMAGE_REVISION="$(
  docker image inspect \
    --format '{{index .Config.Labels "org.opencontainers.image.revision"}}' \
    "${IMAGE_ID}" 2>/dev/null || true
)"
if [[ ! "${IMAGE_REVISION}" =~ ^[0-9a-f]{40}$ ]]; then
  echo "experiment image is missing a full source revision label: ${IMAGE}" >&2
  exit 2
fi
if (( NEEDS_JUDGE == 1 )); then
  SOURCE_HEAD="$(git -C "${ROOT_DIR}" rev-parse --verify HEAD 2>/dev/null || true)"
  if [[ "${IMAGE_REVISION}" != "${SOURCE_HEAD}" ]]; then
    echo "experiment image revision does not match the launcher worktree" >&2
    exit 2
  fi
fi

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

mkdir -p "${ROOT_DIR}/runs"

DOCKER_ARGS=(
  --rm
  --init
  --read-only
  --network host
  --memory "${MEMORY}"
  --pids-limit "${PIDS_LIMIT}"
  --cap-drop ALL
  --security-opt no-new-privileges=true
  --user "${RUN_UID}:${RUN_GID}"
  --tmpfs "/run:rw,nosuid,nodev,exec,size=${RUNTIME_TMPFS_SIZE},mode=0700,uid=${RUN_UID},gid=${RUN_GID}"
  --tmpfs "/tmp:rw,nosuid,nodev,noexec,size=${TMP_TMPFS_SIZE},mode=1777"
  # Code, prompts, manifests, and benchmark inputs come from the immutable
  # image built for this run.  Mount only the output subtree so a host-side
  # worktree edit cannot change later agents in the same experiment.
  -v "${ROOT_DIR}/runs:/opt/contextswarm/runs"
  -e "HOME=/run/contextswarm-mini/home"
  -e "TMPDIR=/tmp"
  -e "MINI_SWARM_NUROUTER_VERSION=${NUROUTER_VERSION}"
  -e "CONTEXTSWARM_IMAGE_ID=${IMAGE_ID}"
  -e "CONTEXTSWARM_IMAGE_REVISION=${IMAGE_REVISION}"
  -e "CONTEXTSWARM_LAUNCH_CONTRACT_REQUIRED=1"
  -e "CONTEXTSWARM_MANIFEST_PATH=${CONFIG}"
  -e "CONTEXTSWARM_MANIFEST_SHA256=${MANIFEST_SHA256}"
)

# Passing only the variable name keeps the private value out of docker's argv
# and command summaries.  Xtrace is disabled above before the value is read.
# Docker copies the value from this launcher's environment.
if [[ -n "${CONTEXTSWARM_JUDGE_URL:-}" ]]; then
  DOCKER_ARGS+=(-e CONTEXTSWARM_JUDGE_URL)
fi
if [[ -n "${CONTEXTSWARM_JUDGE_CACHE_HEALTH_URL:-}" ]]; then
  DOCKER_ARGS+=(-e CONTEXTSWARM_JUDGE_CACHE_HEALTH_URL)
fi
if [[ -n "${LEAN_AUTH_TOKEN:-}" ]]; then
  DOCKER_ARGS+=(-e LEAN_AUTH_TOKEN)
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
    DOCKER_ARGS+=(
      -v "${CODEX_HOME}:/opt/contextswarm-input/codex-home:ro"
      -e "MINI_SWARM_CODEX_INPUT_ENABLED=1"
    )
  fi
fi

exec docker run "${DOCKER_ARGS[@]}" "${IMAGE_ID}" \
  --config "${CONFIG}" "${COMMAND}" "${ARGS[@]}"
