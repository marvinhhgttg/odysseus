#!/usr/bin/env python3

import argparse
import json
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "evals" / "suites.json"
DEFAULT_REPORT = ROOT / ".artifacts" / "evals" / "latest.json"


def load_config(path):
    with path.open(encoding="utf-8") as handle:
        config = json.load(handle)

    suites = config.get("suites")
    if config.get("version") != 1 or not isinstance(suites, dict) or not suites:
        raise ValueError("invalid eval suite configuration")

    for name, suite in suites.items():
        tests = suite.get("tests") if isinstance(suite, dict) else None
        if not isinstance(tests, list) or not tests:
            raise ValueError(f"suite {name!r} has no tests")

    return config


def parse_junit(path):
    root = ET.parse(path).getroot()
    nodes = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))

    totals = {
        "collected": 0,
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
    }
    failures = []

    for node in nodes:
        tests = int(node.attrib.get("tests", 0))
        failed = int(node.attrib.get("failures", 0))
        errors = int(node.attrib.get("errors", 0))
        skipped = int(node.attrib.get("skipped", 0))

        totals["collected"] += tests
        totals["failed"] += failed
        totals["errors"] += errors
        totals["skipped"] += skipped

        for case in node.findall("testcase"):
            problem = case.find("failure")
            kind = "failure"
            if problem is None:
                problem = case.find("error")
                kind = "error"
            if problem is None:
                continue
            failures.append(
                {
                    "nodeid": "::".join(
                        value
                        for value in (
                            case.attrib.get("classname"),
                            case.attrib.get("name"),
                        )
                        if value
                    ),
                    "kind": kind,
                    "message": problem.attrib.get("message", ""),
                }
            )

    totals["passed"] = max(
        0,
        totals["collected"]
        - totals["failed"]
        - totals["errors"]
        - totals["skipped"],
    )
    return totals, failures


def run_suite(name, definition, extra_pytest_args):
    test_paths = [ROOT / test for test in definition["tests"]]
    missing = [
        str(path.relative_to(ROOT))
        for path in test_paths
        if not path.is_file()
    ]

    started = time.monotonic()

    if missing:
        return {
            "name": name,
            "description": definition.get("description", ""),
            "status": "error",
            "duration_seconds": round(time.monotonic() - started, 3),
            "exit_code": 4,
            "counts": {
                "collected": 0,
                "passed": 0,
                "failed": 0,
                "errors": len(missing),
                "skipped": 0,
            },
            "missing_tests": missing,
            "failures": [],
        }

    with tempfile.TemporaryDirectory(prefix=f"odysseus-eval-{name}-") as tmp:
        junit = Path(tmp) / "junit.xml"
        command = [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--disable-warnings",
            f"--junitxml={junit}",
            *[str(path.relative_to(ROOT)) for path in test_paths],
            *extra_pytest_args,
        ]
        completed = subprocess.run(command, cwd=ROOT, check=False)

        counts = {
            "collected": 0,
            "passed": 0,
            "failed": 0,
            "errors": 0,
            "skipped": 0,
        }
        failures = []
        parse_error = None

        if junit.is_file():
            try:
                counts, failures = parse_junit(junit)
            except (ET.ParseError, OSError, ValueError) as exc:
                parse_error = str(exc)
        else:
            parse_error = "pytest did not create a JUnit report"

    result = {
        "name": name,
        "description": definition.get("description", ""),
        "status": "passed" if completed.returncode == 0 else "failed",
        "duration_seconds": round(time.monotonic() - started, 3),
        "exit_code": completed.returncode,
        "counts": counts,
        "failures": failures,
    }
    if parse_error:
        result["report_error"] = parse_error
        result["status"] = "error"
    return result


def aggregate(results):
    counts = {
        "collected": 0,
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
    }
    for result in results:
        for key in counts:
            counts[key] += result["counts"][key]
    return counts


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Run reproducible Odysseus evaluation suites."
    )
    parser.add_argument(
        "suites",
        nargs="*",
        help="Suite names; defaults to all configured suites.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
    )
    parser.add_argument(
        "--report",
        type=Path,
        default=DEFAULT_REPORT,
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List configured suites and exit.",
    )
    parser.add_argument(
        "--pytest-arg",
        action="append",
        default=[],
        help="Additional argument passed to pytest; repeat as needed.",
    )
    args = parser.parse_args(argv)

    try:
        config = load_config(args.config)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))

    configured = config["suites"]

    if args.list:
        for name, definition in configured.items():
            print(f"{name}: {definition.get('description', '')}")
        return 0

    selected = args.suites or list(configured)
    unknown = [name for name in selected if name not in configured]
    if unknown:
        parser.error("unknown suite(s): " + ", ".join(unknown))

    started_at = datetime.now(timezone.utc)
    started = time.monotonic()
    results = []

    for name in selected:
        print(f"\n=== eval: {name} ===", flush=True)
        results.append(
            run_suite(name, configured[name], args.pytest_arg)
        )

    successful = all(result["status"] == "passed" for result in results)
    report = {
        "schema_version": 1,
        "status": "passed" if successful else "failed",
        "started_at": started_at.isoformat(),
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "duration_seconds": round(time.monotonic() - started, 3),
        "selected_suites": selected,
        "counts": aggregate(results),
        "suites": results,
    }

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    counts = report["counts"]
    print(
        "\n"
        f"status={report['status']} "
        f"collected={counts['collected']} "
        f"passed={counts['passed']} "
        f"failed={counts['failed']} "
        f"errors={counts['errors']} "
        f"skipped={counts['skipped']}"
    )
    print(f"report={args.report}")

    return 0 if successful else 1


if __name__ == "__main__":
    raise SystemExit(main())
