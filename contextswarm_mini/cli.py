"""Command-line entrypoint for the compact experiment harness."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .config import ConfigError, load_config
from .preflight import run_preflight
from .runner import load_tasks, plan, run_experiment


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="contextswarm-mini")
    parser.add_argument("--config", default="configs/cps.toml", help="manifest path or configs/<name>.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    plan_cmd = sub.add_parser("plan", help="validate and print the session plan")
    plan_cmd.add_argument("--json", action="store_true", dest="as_json")

    validate = sub.add_parser("validate", help="validate the dataset and local manifest only")
    validate.add_argument("--json", action="store_true", dest="as_json")

    preflight = sub.add_parser("preflight", help="check NuRouter/AISW and Lean transport without starting Pi")
    preflight.add_argument("--output", type=Path, default=Path("runs/preflight"))

    run = sub.add_parser("run", help="run one experiment cell")
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--mock-agent", action="store_true", help="offline harness smoke; does not call Pi")
    run.add_argument("--mock-proved", action="store_true", help="mock evaluator accepts candidates without sorry")
    run.add_argument("--output", type=Path, default=None, help="override the manifest output root")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        config = load_config(args.config)
        tasks = load_tasks(config)
        if args.command == "plan":
            payload = plan(config, tasks)
            _print(payload, args.as_json)
            return 0
        if args.command == "validate":
            payload = {
                "ok": True,
                "dataset": "matholympiadbench",
                "task_count": len(tasks),
                "tasks": [task.slug for task in tasks],
                "manifest": str(config.manifest_path),
            }
            _print(payload, args.as_json)
            return 0
        if args.command == "preflight":
            payload = run_preflight(config, args.output.resolve())
            _print(payload, True)
            return 0
        run_dir = run_experiment(
            config,
            dry_run=args.dry_run,
            mock_agent=args.mock_agent,
            mock_proved=args.mock_proved,
            output_override=args.output,
        )
        print(run_dir)
        return 0
    except (ConfigError, ValueError, OSError, RuntimeError) as exc:
        print(f"contextswarm-mini: {exc}", file=sys.stderr)
        return 2


def _print(payload: dict[str, object], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        for key, value in payload.items():
            if isinstance(value, list):
                print(f"{key}: {', '.join(map(str, value))}")
            else:
                print(f"{key}: {value}")


if __name__ == "__main__":
    raise SystemExit(main())
