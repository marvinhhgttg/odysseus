"""Coverage for the prompt-injection eval CLI (evals/run_injection_tests.py)."""
import importlib.util
import json
import os
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "evals" / "run_injection_tests.py"

SPEC = importlib.util.spec_from_file_location("run_injection_tests", SCRIPT)
cli = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(cli)


@pytest.fixture
def injection_config(tmp_path):
    path = tmp_path / "suites.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "suites": {
                    "prompt_injection": {
                        "description": "Live-model eval suite",
                        "tests": ["tests/test_eval_suite_runner.py"],
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    return path


def test_constants_point_at_repo_paths():
    assert cli.SUITE_NAME == "prompt_injection"
    assert cli.DEFAULT_CONFIG == ROOT / "evals" / "suites.json"
    assert cli.DEFAULT_REPORT == (
        ROOT / "evals" / "results" / "prompt_injection" / "latest.json"
    )
    assert (ROOT / "scripts" / "run_evals.py").is_file()


def test_configured_injection_suite_files_exist():
    suite = cli.load_config(cli.DEFAULT_CONFIG)["suites"]["prompt_injection"]
    assert suite["tests"]
    for relative_path in suite["tests"]:
        assert (ROOT / relative_path).is_file(), relative_path


def test_list_prints_suite_files(injection_config, capsys):
    assert cli.main(["--config", str(injection_config), "--list"]) == 0
    out = capsys.readouterr().out
    assert "prompt_injection" in out
    assert "tests/test_eval_suite_runner.py" in out


def test_offline_run_is_all_skipped_clean(tmp_path, monkeypatch):
    monkeypatch.delenv(cli.LIVE_ENV, raising=False)
    report = tmp_path / "latest.json"
    assert cli.main(
        ["--config", str(cli.DEFAULT_CONFIG), "--report", str(report)]
    ) == 0
    assert cli.all_skipped(report) is True
    assert cli.all_skipped(tmp_path / "missing.json") is False


def test_live_flag_sets_env_var(injection_config, tmp_path, monkeypatch):
    monkeypatch.delenv(cli.LIVE_ENV, raising=False)
    report = tmp_path / "latest.json"
    assert cli.main(
        ["--live", "--config", str(injection_config), "--report", str(report)]
    ) == 0
    assert os.environ.get(cli.LIVE_ENV) == "1"


def test_fail_if_skipped_passes_when_cases_ran(injection_config, tmp_path, monkeypatch):
    monkeypatch.delenv(cli.LIVE_ENV, raising=False)
    report = tmp_path / "latest.json"
    # Tiny suite actually passes (nothing skipped) -> --fail-if-skipped stays 0.
    assert cli.main(
        [
            "--fail-if-skipped",
            "--config",
            str(injection_config),
            "--report",
            str(report),
        ]
    ) == 0
    assert cli.all_skipped(report) is False


def _fake_report(tmp_path, counts):
    path = tmp_path / "latest.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"counts": counts}),
        encoding="utf-8",
    )
    return path


def test_all_skipped_true_only_for_full_skip(tmp_path):
    skipped = _fake_report(
        tmp_path / "s", {"collected": 90, "passed": 0, "failed": 0, "errors": 0, "skipped": 90}
    )
    assert cli.all_skipped(skipped) is True

    partial = _fake_report(
        tmp_path / "p", {"collected": 90, "passed": 1, "failed": 0, "errors": 0, "skipped": 89}
    )
    assert cli.all_skipped(partial) is False

    empty = _fake_report(tmp_path / "e", {"collected": 0, "passed": 0, "failed": 0, "errors": 0, "skipped": 0})
    assert cli.all_skipped(empty) is False

    malformed = tmp_path / "m" / "latest.json"
    assert cli.all_skipped(malformed) is False