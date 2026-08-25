# Evaluation and quality tooling

This directory contains non-production code that exercises the real application through its public
services and adapters. The dependency direction is intentionally one-way: `evaluation` may import
`app`, while `app` must never import `evaluation` or `tests`.

Install local quality dependencies with:

```bash
python -m pip install -e '.[dev,quality]'
```

Main entry points:

```bash
python -m evaluation.business --help
python -m evaluation.benchmark.cli --help
python -m evaluation.smoke.pgvector --help
```

- `benchmark/` contains deterministic synthetic corpora and benchmark runners.
- `datasets/` contains checked-in labelled questions; real business sets should be access-controlled.
- `smoke/` contains destructive-isolated infrastructure verification.
- `scripts/` contains corpus generators and repeatable quality gates.
- `reports/` contains auditable historical summaries.
- `generated/` contains ignored local artifacts and must not be committed.

Production wheels and runtime images package only `app`. The Docker `quality-runtime` target is the
explicit environment for containerized evaluation.
