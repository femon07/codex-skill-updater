#!/usr/bin/env python3
"""Run check+apply in one command.

Default: no artifact files generated.
Debug mode: writes fixed artifact files in current directory.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


SCRIPT_DIR = Path(__file__).resolve().parent
CHECK_SCRIPT = SCRIPT_DIR / "check_skill_updates.py"
APPLY_SCRIPT = SCRIPT_DIR / "apply_skill_updates.py"
DEBUG_CHECK_FILE = "skill_update_check.debug.tsv"
DEBUG_REPORT_FILE = "skill_update_apply_report.debug.json"


def _run(cmd: list[str], input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, input=input_text, capture_output=True, check=False)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run check+apply skill updates.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--allow-manual-map", action="store_true")
    parser.add_argument("--source-map", default="")
    parser.add_argument("--fail-fast", action="store_true")
    parser.add_argument("--strategy", action="append", default=[])
    parser.add_argument("--skill", action="append", default=[])
    parser.add_argument("--debug-artifacts", action="store_true")
    parser.add_argument("--jobs", type=int, default=4)
    return parser.parse_args(argv)


def main(argv: list[str]) -> int:
    args = _parse_args(argv)

    env = os.environ.copy()
    if not env.get("GH_TOKEN") and not env.get("GITHUB_TOKEN"):
        gh_token = _run(["gh", "auth", "token"])
        if gh_token.returncode == 0 and gh_token.stdout.strip():
            env["GH_TOKEN"] = gh_token.stdout.strip()

    check_proc = subprocess.run(
        ["python3", str(CHECK_SCRIPT), "--jobs", str(args.jobs)],
        text=True,
        capture_output=True,
        check=False,
        env=env,
    )
    if check_proc.returncode != 0:
        if check_proc.stdout:
            print(check_proc.stdout, end="", file=sys.stdout)
        if check_proc.stderr:
            print(check_proc.stderr, end="", file=sys.stderr)
        return check_proc.returncode

    check_output = check_proc.stdout
    if args.debug_artifacts:
        Path(DEBUG_CHECK_FILE).write_text(check_output, encoding="utf-8")

    apply_cmd = ["python3", str(APPLY_SCRIPT), "--check-file", "-", "--jobs", str(args.jobs)]
    if args.dry_run:
        apply_cmd.append("--dry-run")
    if args.allow_manual_map:
        apply_cmd.append("--allow-manual-map")
    if args.source_map:
        apply_cmd.extend(["--source-map", args.source_map])
    if args.fail_fast:
        apply_cmd.append("--fail-fast")
    if args.debug_artifacts:
        apply_cmd.extend(["--report", DEBUG_REPORT_FILE])
    for strategy in args.strategy:
        apply_cmd.extend(["--strategy", strategy])
    for skill in args.skill:
        apply_cmd.extend(["--skill", skill])

    apply_proc = _run(apply_cmd, input_text=check_output)
    if apply_proc.stdout:
        print(apply_proc.stdout, end="", file=sys.stdout)
    if apply_proc.stderr:
        print(apply_proc.stderr, end="", file=sys.stderr)
    return apply_proc.returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
