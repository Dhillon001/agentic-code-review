# Agentic Code Review

Multi-agent PR reviewer: parallel specialist agents (correctness, security, performance, tests) plus a
verifier pass that re-checks every finding before it's posted. Precision over volume.

**Status:** work in progress — ingest layer (diff parsing, GitHub client, code sources) is in.

```bash
pip install -e ".[dev]" && pytest -q
```
