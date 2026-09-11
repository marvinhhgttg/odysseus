#!/usr/bin/env python3
"""CLI for the live prompt-injection eval suite.

Wraps ``scripts/run_evals.py`` and runs the ``prompt_injection`` suite. By
default the suite is reported as all-skipped (live evals require a configured
local MLX/Ollama model and make real LLM calls). Pass ``--live`` to set
``ODYSSEUS_RUN_LIVE_EVALS=1`` so the model cases actually run; the report is
written to ``evals/results/prompt_injection/latest.json``.

Offline it exits 0 with an all-skipped report (CI-safe). Use
``--fail-if-skipped`` when live evals were explicitly intended, so a fully
skipped run fails loudly instead of masquerading as a pass.

Forwarded pytest arguments must come after ``--``:

    python evals/run_injection_tests.py --live -- -x
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "evals" / "suites.json"
DEFAULT_REPORT = ROOT / "evals" / "results" / "prompt_injection" / "latest.json"
SUITE_NAME = "prompt_injection"
LIVE_ENV = "ODYSSEUS_RUN_LIVE_EVALS"


def _load_run_evals():
    """Import the shared runner from its path (same approach as the tests)."""
    path = ROOT / "scripts" / "run_evals.py"
    spec = importlib.util.spec_from_file_location("run_evals", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def load_config(path: Path) -> dict:
    """Load the suite config, failing fast if the injection suite is missing."""
    config = json.loads(path.read_text(encoding="utf-8"))
    if SUITE_NAME not in config.get("suites", {}):
        raise ValueError(f"config has no {SUITE_NAME!r} suite")
    return config


def all_skipped(report_path: Path) -> bool:
    """True when the written report shows every collected case skipped.

    Only counts when at least one case was collected, so an empty/no-report
    is never misread as "all skipped".
    """
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    counts = report.get("counts") or {}
    collected = int(counts.get("collected", 0) or 0)
    if collected <= 0:
        return False
    return (
        int(counts.get("skipped", 0) or 0) == collected
        and int(counts.get("passed", 0) or 0) == 0
        and int(counts.get("failed", 0) or 0) == 0
        and int(counts.get("errors", 0) or 0) == 0
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the live prompt-injection eval suite."
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=f"Set {LIVE_ENV}=1 so real-model cases run instead of being skipped.",
    )
    parser.add_argument(
        "--fail-if-skipped",
        action="store_true",
        help="Exit 1 when every collected case was skipped (no live calls ran).",
    )
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument(
        "--list",
        action="store_true",
        help="List the injection suite's test files and exit.",
    )
    parser.add_argument(
        "pytest_args",
        nargs=argparse.REMAINDER,
        help="Arguments forwarded to pytest, after --.",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))

    if args.list:
        suite = config["suites"][SUITE_NAME]
        print(f"{SUITE_NAME}: {suite.get('description', '')}")
        for test in suite["tests"]:
            print(f"  {test}")
        return 0

    if args.live:
        os.environ[LIVE_ENV] = "1"
    else:
        print(
            f"notes: {LIVE_ENV} not enabled; cases will be skipped. "
            f"Use --live to run them."
        )

    runner_args = [
        SUITE_NAME,
        "--config",
        str(args.config),
        "--report",
        str(args.report),
    ]
    for arg in args.pytest_args:
        runner_args.append(f"--pytest-arg={arg}")

    exit_code = _load_run_evals().main(runner_args)

    if args.fail_if_skipped and exit_code == 0 and all_skipped(args.report):
        print(
            f"FAIL: every collected {SUITE_NAME} case was skipped; "
            "live eval did not run."
        )
        return 1
    return exit_code


if __name__ == "__main__":
    sys.exit(main())