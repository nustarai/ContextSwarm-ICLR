#!/usr/bin/env bash
# Operator capabilities are environment-only; never include them in tracing.
{ set +x; } 2>/dev/null
set -euo pipefail

ROOT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
# Keep output independent of disposable source worktrees. The Docker launcher
# checks that the chosen subtree cannot expose a source checkout or ancestor.
export CONTEXTSWARM_MINI_RUNS_ROOT="${CONTEXTSWARM_MINI_RUNS_ROOT:-${ROOT_DIR}/../ContextSwarm-ICLR-experiments/runs}"
usage() {
  echo 'Usage: bash scripts/run_revalidation.sh issue38 SELECTOR | issue39 ALLOCATOR | parallel | smoke38 | smoke39 | smoke-parallel [--mock-agent]'
}
if [[ $# -eq 0 ]]; then usage >&2; exit 2; fi
FAMILY="$1"
shift
EXTRA=()
if [[ "${*: -1}" == '--mock-agent' ]]; then
  EXTRA=(--mock-agent)
  set -- "${@:1:$#-1}"
fi
case "$FAMILY" in
  issue38)
    [[ $# -eq 1 ]] || { usage >&2; exit 2; }
    case "$1" in recency|random|bm25_mmr|smoothed_popularity|feedback_diversity|no_interaction_feedback|unnormalized_feedback|nustigmergy) ;; *) usage >&2; exit 2;; esac
    MANIFEST="configs/issue38_formal/matholympiadbench/repeat1/$1.toml"
    OUTPUT="runs/revalidation/issue38/matholympiadbench/repeat1/$1"
    ;;
  issue39)
    [[ $# -eq 1 ]] || { usage >&2; exit 2; }
    case "$1" in uniform_refill|task_state|trace_state|llm_scheduler) ;; *) usage >&2; exit 2;; esac
    MANIFEST="configs/figure4_formal_6datasets/matholympiadbench/repeat1/$1.toml"
    OUTPUT="runs/revalidation/issue39/matholympiadbench/repeat1/$1"
    ;;
  parallel)
    [[ $# -eq 0 ]] || { usage >&2; exit 2; }
    MANIFEST='configs/revalidation_matholympiadbench/parallel.toml'
    OUTPUT='runs/revalidation/baseline/matholympiadbench/repeat1/parallel'
    ;;
  smoke38|smoke39|smoke-parallel)
    [[ $# -eq 0 ]] || { usage >&2; exit 2; }
    case "$FAMILY" in smoke38) KIND=issue38;; smoke39) KIND=issue39;; smoke-parallel) KIND=parallel;; esac
    MANIFEST="configs/revalidation_matholympiadbench/smoke_$KIND.toml"
    OUTPUT="runs/maintenance_smoke/$KIND"
    ;;
  *) usage >&2; exit 2;;
esac

if [[ ${#EXTRA[@]} -eq 0 ]]; then
  : "${CONTEXTSWARM_MINI_IMAGE:?Build and pin an image from the current clean commit first}"
  : "${CONTEXTSWARM_JUDGE_URL:?Set the operator-owned Judge endpoint}"
  export CONTEXTSWARM_JUDGE_CACHE_HEALTH_URL="${CONTEXTSWARM_JUDGE_CACHE_HEALTH_URL:-$CONTEXTSWARM_JUDGE_URL}"
  if [[ -z "${CONTEXTSWARM_MINI_DECL_INDEX:-}" ]]; then
    INDEX_ROOT="${CONTEXTSWARM_FORMAL_INDEX_DIR:-${ROOT_DIR}/../contextswarm-formal-index-iclr}"
    export CONTEXTSWARM_MINI_DECL_INDEX="${INDEX_ROOT}/mathlib-v4.9-34ffb0c1cb04e65a6d2e74a9433884cef467bc17.sqlite3"
    export CONTEXTSWARM_MINI_DECL_INDEX_SHA256='550994047b88d73839afccc71ba6056199e4e2fc5f3b4d1d05389c904176218e'
    export CONTEXTSWARM_MINI_MATHLIB_REVISION='34ffb0c1cb04e65a6d2e74a9433884cef467bc17'
  fi
fi

exec bash "$ROOT_DIR/scripts/run_docker.sh" --config "$MANIFEST" --output "$OUTPUT" "${EXTRA[@]}"
