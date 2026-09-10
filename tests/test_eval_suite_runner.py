import importlib.util
import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_evals.py"

SPEC = importlib.util.spec_from_file_location("run_evals", SCRIPT)
run_evals = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(run_evals)


def test_eval_config_has_no_duplicate_json_keys():
    def reject_duplicates(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON key: {key}")
            result[key] = value
        return result

    json.loads(
        (ROOT / "evals" / "suites.json").read_text(encoding="utf-8"),
        object_pairs_hook=reject_duplicates,
    )


def test_configured_eval_files_exist():
    config = run_evals.load_config(ROOT / "evals" / "suites.json")

    assert set(config["suites"]) == {
        "approval",
        "routing",
        "grounding",
        "prompt_safety",
        "prompt_injection",
        "runtime",
        "workflows",
    }

    for suite in config["suites"].values():
        for relative_path in suite["tests"]:
            assert (ROOT / relative_path).is_file(), relative_path


def test_parse_junit_counts_and_failures(tmp_path):
    junit = tmp_path / "junit.xml"
    junit.write_text(
        """
        <testsuites>
          <testsuite tests="4" failures="1" errors="1" skipped="1">
            <testcase classname="tests.test_demo" name="test_pass"/>
            <testcase classname="tests.test_demo" name="test_fail">
              <failure message="assertion failed"/>
            </testcase>
            <testcase classname="tests.test_demo" name="test_error">
              <error message="setup failed"/>
            </testcase>
            <testcase classname="tests.test_demo" name="test_skip">
              <skipped/>
            </testcase>
          </testsuite>
        </testsuites>
        """,
        encoding="utf-8",
    )

    counts, failures = run_evals.parse_junit(junit)

    assert counts == {
        "collected": 4,
        "passed": 1,
        "failed": 1,
        "errors": 1,
        "skipped": 1,
    }
    assert [failure["kind"] for failure in failures] == [
        "failure",
        "error",
    ]


def test_missing_test_fails_closed():
    result = run_evals.run_suite(
        "missing",
        {
            "description": "Missing fixture",
            "tests": ["tests/does_not_exist.py"],
        },
        [],
    )

    assert result["status"] == "error"
    assert result["exit_code"] != 0
    assert result["counts"]["errors"] == 1


def test_main_writes_machine_readable_report(tmp_path, monkeypatch):
    config = tmp_path / "suites.json"
    report = tmp_path / "report.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "suites": {
                    "sample": {
                        "description": "Sample",
                        "tests": ["tests/test_eval_suite_runner.py"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    def fake_run_suite(name, definition, extra_pytest_args):
        return {
            "name": name,
            "description": definition["description"],
            "status": "passed",
            "duration_seconds": 0.01,
            "exit_code": 0,
            "counts": {
                "collected": 2,
                "passed": 2,
                "failed": 0,
                "errors": 0,
                "skipped": 0,
            },
            "failures": [],
        }

    monkeypatch.setattr(run_evals, "run_suite", fake_run_suite)

    exit_code = run_evals.main(
        [
            "sample",
            "--config",
            str(config),
            "--report",
            str(report),
        ]
    )

    data = json.loads(report.read_text(encoding="utf-8"))
    assert exit_code == 0
    assert data["status"] == "passed"
    assert data["selected_suites"] == ["sample"]
    assert data["counts"]["passed"] == 2


def test_unknown_suite_exits_nonzero(tmp_path):
    config = tmp_path / "suites.json"
    config.write_text(
        json.dumps(
            {
                "version": 1,
                "suites": {
                    "sample": {
                        "description": "Sample",
                        "tests": ["tests/test_eval_suite_runner.py"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(SystemExit) as exc:
        run_evals.main(
            [
                "unknown",
                "--config",
                str(config),
                "--report",
                str(tmp_path / "report.json"),
            ]
        )

    assert exc.value.code != 0


def test_ci_workflow_runs_and_uploads_quality_evals():
    workflow = (
        ROOT / ".github" / "workflows" / "ci.yml"
    ).read_text(encoding="utf-8")

    assert "  quality-evals:\n" in workflow
    assert "run: python scripts/run_evals.py" in workflow
    assert "name: quality-eval-report" in workflow
    assert "path: .artifacts/evals/latest.json" in workflow
    assert "if: ${{ always() }}" in workflow
    assert "if-no-files-found: warn" in workflow
