# Odysseus evaluation suites

The evaluation runner groups existing regression tests into stable,
product-level quality gates.

## Suites

- `approval`: Tool approval persistence, enforcement, routes and resume.
- `routing`: Deterministic model routing and request integration.
- `grounding`: Claim-level web grounding and source attribution.

Suite membership is declared in `evals/suites.json`.

## Run all suites

```bash
python scripts/run_evals.py
```

The default machine-readable report is written to:

```text
.artifacts/evals/latest.json
```

## Run selected suites

```bash
python scripts/run_evals.py approval
python scripts/run_evals.py routing grounding
```

## Write a custom report

```bash
python scripts/run_evals.py \
  --report /tmp/odysseus-eval-report.json
```

## List suites

```bash
python scripts/run_evals.py --list
```

## Forward Pytest options

Repeat `--pytest-arg` for each option:

```bash
python scripts/run_evals.py approval \
  --pytest-arg=-x \
  --pytest-arg=--tb=short
```

## Exit codes

- `0`: Every selected suite passed.
- `1`: At least one selected suite failed or could not produce a report.
- `2`: Invalid command-line arguments or an invalid configuration.

Missing configured test files fail closed and make the evaluation fail.
