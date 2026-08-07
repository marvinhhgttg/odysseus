# Odysseus evaluation suites

The evaluation runner groups existing regression tests into stable,
product-level quality gates.

## Suites

- `approval`: Tool approval persistence, enforcement, routes, resume, and
  browser-side recovery after a reload.
- `routing`: Deterministic model routing and request integration.
- `runtime`: Real application boot in a clean process — route surface,
  degraded-dependency startup, and checkout isolation.
- `grounding`: Claim-level web grounding and source attribution.

Suite membership is declared in `evals/suites.json`.

The `approval` suite includes a Node-backed frontend test. It is skipped when
`node` is not on `PATH`, so CI installs Node explicitly rather than relying on
the runner image.

The `runtime` suite boots `app.py` in a subprocess against a throwaway data
directory, started from outside the repository. It is the slowest suite by an
order of magnitude and the one that catches fresh-install breakage — a missing
dependency in the import graph, a circular import, or a router that stopped
being registered.

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

## Continuous integration

The blocking `Quality evals` job in `.github/workflows/ci.yml` runs all
configured suites on pushes to `main` and on pull requests.

The job uploads `.artifacts/evals/latest.json` as the
`quality-eval-report` artifact, including after a failed evaluation run when
the report was successfully created.
