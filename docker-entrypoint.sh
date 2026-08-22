#!/usr/bin/env bash
set -euo pipefail
umask 077

# NuRouter/AISW managed launchers require adjacent owner-only metadata. Host
# files are input only; copy them into a container-private root first.
RUNTIME_ROOT="${MINI_SWARM_RUNTIME_ROOT:-/run/contextswarm-mini}"
RUNTIME_HOME="${MINI_SWARM_HOME:-${RUNTIME_ROOT}/home}"
INPUT_ROOT="${MINI_SWARM_AISW_INPUT_ROOT:-/opt/contextswarm-input/aisw}"
INPUT_BINARY="${MINI_SWARM_PI_INPUT_BINARY:-${INPUT_ROOT}/pi}"
INPUT_NODE_CONFIG="${MINI_SWARM_AISW_INPUT_NODE_CONFIG:-/opt/contextswarm-input/aisw-private/node.toml}"
INPUT_CODEX_HOME="${MINI_SWARM_CODEX_INPUT_HOME:-/opt/contextswarm-input/codex-home}"

install -d -m 0700 \
  "${RUNTIME_ROOT}" \
  "${RUNTIME_HOME}" \
  "${RUNTIME_ROOT}/cache" \
  "${RUNTIME_ROOT}/config" \
  "${RUNTIME_ROOT}/data" \
  "${RUNTIME_ROOT}/xdg" \
  "${RUNTIME_HOME}/.codex"

export HOME="${RUNTIME_HOME}"
export XDG_CACHE_HOME="${RUNTIME_ROOT}/cache"
export XDG_CONFIG_HOME="${RUNTIME_ROOT}/config"
export XDG_DATA_HOME="${RUNTIME_ROOT}/data"
export XDG_RUNTIME_DIR="${RUNTIME_ROOT}/xdg"
export TMPDIR="${TMPDIR:-/tmp}"

CODEX_RUNTIME_HOME="${RUNTIME_HOME}/.codex"
if [[ "${MINI_SWARM_CODEX_INPUT_ENABLED:-0}" == "1" ]]; then
  if [[ ! -d "${INPUT_CODEX_HOME}" ]]; then
    echo "configured Codex home mount is unavailable" >&2
    exit 2
  fi
  CODEX_RUNTIME_HOME="${INPUT_CODEX_HOME}"
fi
export CODEX_HOME="${CODEX_RUNTIME_HOME}"

if [[ -x "${INPUT_BINARY}" ]]; then
  install -d -m 0700 "${RUNTIME_ROOT}/bin" "${RUNTIME_ROOT}/aisw"
  install -m 0755 "${INPUT_BINARY}" "${RUNTIME_ROOT}/bin/pi"

  metadata=""
  for candidate in \
    "${INPUT_ROOT}/.aisw-pi-launcher.json" \
    "${INPUT_ROOT}/.nurouter-pi-launcher.json"
  do
    if [[ -f "${candidate}" ]]; then
      metadata="${candidate}"
      break
    fi
  done
  if [[ -n "${metadata}" ]]; then
    python3 - "${metadata}" "${RUNTIME_ROOT}/bin" "${RUNTIME_ROOT}/aisw" <<'PY'
import json
import os
from pathlib import Path
import sys

source = Path(sys.argv[1])
destination = Path(sys.argv[2])
runtime_home = sys.argv[3]
payload = json.loads(source.read_text(encoding="utf-8"))
if "aisw_home" in payload:
    payload["aisw_home"] = runtime_home
if "nurouter_home" in payload:
    payload["nurouter_home"] = runtime_home
target = destination / source.name
target.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
os.chmod(target, 0o600)
PY
  fi

  export MINI_SWARM_PI_BIN="${RUNTIME_ROOT}/bin/pi"
  export AISW_HOME="${RUNTIME_ROOT}/aisw"
  export CONTEXTSWARM_REAL_PI_BINARY="${CONTEXTSWARM_REAL_PI_BINARY:-/usr/local/bin/pi}"
  export CONTEXTSWARM_AISW_PRIVATE_HOME_REQUIRED=1
  export AISW_DISABLE_LOCAL_FALLBACK=1
fi

if [[ -s "${INPUT_NODE_CONFIG}" ]]; then
  install -d -m 0700 "${RUNTIME_ROOT}/aisw"
  install -m 0600 "${INPUT_NODE_CONFIG}" "${RUNTIME_ROOT}/aisw/node.toml"
  python3 - "${RUNTIME_ROOT}/aisw/node.toml" "${CODEX_RUNTIME_HOME}" <<'PY'
import json
from pathlib import Path
import sys

path = Path(sys.argv[1])
codex_home = sys.argv[2]
lines = path.read_text(encoding="utf-8").splitlines()
table = ""
seen_pi = False
seen_codex = False
for index, line in enumerate(lines):
    stripped = line.strip()
    if stripped.startswith("[") and stripped.endswith("]"):
        table = stripped[1:-1].strip()
    if stripped.startswith("real_pi") and "=" in stripped:
        lines[index] = 'real_pi = "/usr/local/bin/pi"'
        seen_pi = True
    elif stripped.startswith("real_codex") and "=" in stripped:
        lines[index] = 'real_codex = "/usr/local/bin/codex"'
        seen_codex = True
    elif table == "shared_codex_home" and stripped.startswith("path") and "=" in stripped:
        lines[index] = f"path = {json.dumps(codex_home)}"
if not seen_pi:
    lines.append('real_pi = "/usr/local/bin/pi"')
if not seen_codex:
    lines.append('real_codex = "/usr/local/bin/codex"')
path.write_text("\n".join(lines) + "\n", encoding="utf-8")
PY
  chmod 0600 "${RUNTIME_ROOT}/aisw/node.toml"
  export MINI_SWARM_AISW_NODE_CONFIG="${RUNTIME_ROOT}/aisw/node.toml"
  export AISW_NODE_CONFIG="${RUNTIME_ROOT}/aisw/node.toml"
  export CONTEXTSWARM_AISW_NODE_CONFIG="${RUNTIME_ROOT}/aisw/node.toml"
fi

exec python3 -m contextswarm_mini.cli "$@"
