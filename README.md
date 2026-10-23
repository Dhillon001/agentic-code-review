# Agentic Code Review

Multi-agent PR reviewer. Specialist agents review in parallel; a stronger verifier model re-checks every
finding against the code before anything is posted. The bet: **precision over volume** — a reviewer
people don't mute.

```
 PR / branch
     │
 ┌───▼────────┐   ┌────────────────────┐
 │ 1. Ingest  │──▶│ 2. Context (RAG)   │  changed file + callers of changed symbols
 └────────────┘   └─────────┬──────────┘
                            │  batched to fit prompt budget
        ┌───────────────┬───┴───────────┬───────────────┐
 ┌──────▼─────┐ ┌───────▼────┐ ┌────────▼────┐ ┌────────▼───┐
 │correctness │ │  security  │ │ performance │ │   tests    │   3. Specialists (parallel)
 └──────┬─────┘ └───────┬────┘ └────────┬────┘ └────────┬───┘
        └───────────────┴───────┬───────┴───────────────┘
                     confidence floor + dedupe
                        ┌───────▼────────┐
                        │ 4. Verifier    │  stronger model, per file; no verdict = dropped
                        └───────┬────────┘
                        ┌───────▼────────┐
                        │ 5. Rank + place│  severity floor, cap N, anchor to diff lines
                        └───────┬────────┘
                    inline PR comments + summary
```

## Layout

| Path | What it does |
|---|---|
| `src/reviewer/ingest/` | Diff parser, GitHub client, sources (`GitHubSource`, `LocalGitSource`, `InMemorySource`) |
| `src/reviewer/context/retriever.py` | Builds per-file context. `find_related` is the seam for embedding RAG |
| `src/reviewer/agents/` | `prompts.py` (all wording + tool schemas), `specialists.py`, `verifier.py`, `llm.py` |
| `src/reviewer/pipeline.py` | Orchestration |
| `src/reviewer/ranking.py` | Dedupe / rank / snap-to-diff-line |
| `src/reviewer/output/` | GitHub review formatting + posting |
| `src/reviewer/{cli,server,service}.py` | CLI, FastAPI webhook, shared entry points |
| `evals/` | Labelled cases + precision/recall scorer |
| `tests/` | Offline tests with a `FakeLLM` (no API key needed) |

## Quickstart

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # fill in ANTHROPIC_API_KEY (+ GITHUB_TOKEN for PRs)
pytest -q                     # offline

acr local --base main         # review your current branch
acr pr owner/repo 123         # review a GitHub PR (prints)
acr pr owner/repo 123 --post  # ...and post it
python evals/run_evals.py     # score it; add --no-verify to see what the verifier buys you
```

## Deploy

- **GitHub Action** — move `deploy/code-review.yml` to `.github/workflows/`. For another repo, copy it, change `pip install .`
  to `pip install git+https://github.com/<you>/agentic-code-review`, and add the `ANTHROPIC_API_KEY` secret.
- **Webhook service** — `docker build -t acr . && docker run -p 8000:8000 --env-file .env acr`, point a GitHub
  webhook (pull_request events, JSON, secret = `GITHUB_WEBHOOK_SECRET`) at `/webhook`.

## Design choices

- **Forced tool use** for all model output → structured JSON, no regex parsing.
- **Line-numbered diffs** in prompts so models cite real lines; `place()` snaps near-misses and moves
  off-diff findings to the summary (GitHub rejects the whole review if one inline line is invalid).
- **Missing verdict = rejected.** Conservative on purpose.
- **Rejected findings are kept** in `ReviewResult.rejected` — that's your eval signal for prompt tuning.

## Roadmap

See [ROADMAP.md](ROADMAP.md) — one tested feature per commit toward v0.1.0.
