# Agentic Code Review

Multi-agent PR reviewer: parallel specialist agents (correctness, security, performance, tests) plus a
verifier pass that re-checks every finding before it's posted. Precision over volume.

**Status:** work in progress — ingest, context retrieval, specialist agents, and verifier are in.

```bash
pip install -e ".[dev]" && pytest -q
```

```bash
acr local --base main          # review your current branch
acr pr owner/repo 123 --post   # review + post to a GitHub PR
python evals/run_evals.py      # precision / recall on labelled cases
```
