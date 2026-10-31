# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [0.1.0] — 2026-10-31

First release: a multi-agent pull-request reviewer where four specialists review in parallel and a stronger
verifier model re-checks every finding before anything is posted.

### Core pipeline

- **Diff ingest** — unified-diff parser with new-file line numbers and binary detection; async GitHub REST
  client; `GitHubSource`, `LocalGitSource`, and `InMemorySource` for reading and searching code.
- **Context building** — whole file for short files, ±N-line windows around hunks for long ones, plus related
  call-site snippets for changed symbols; files are greedy-packed into batches that fit the prompt budget.
- **Specialist agents** — correctness, security, performance, and tests reviewers, all output via forced tool use.
- **Verifier** — a stronger model confirms or rejects each finding; a missing verdict counts as a rejection.
  Rejected findings are kept in `ReviewResult.rejected`.
- **Ranking & placement** — confidence floor, cross-agent dedupe, severity floor, hard comment cap, and snapping
  findings onto the nearest commentable diff line (off-diff findings go in the summary).
- **Entry points** — `acr` CLI (`pr`, `local`), FastAPI webhook server with HMAC signature checks, Docker image,
  and a GitHub Action workflow.
- **Evals** — labelled cases and a precision/recall/F1 scorer (`evals/run_evals.py`).

### Added

- **Per-repo config (`.acr.yml`)** — `ignore`, `min_severity`, `max_comments`, and free-text team `rules`
  injected into every reviewer's and the verifier's prompt. Read from the PR's base branch so a PR can't loosen
  its own review; bad config never blocks a review.
- **Noise-file skipping** — lockfiles, minified bundles, source maps, generated code, `dist/`, `vendor/`,
  `node_modules/`, and binary diffs are dropped before context building. Reported as `skipped` in stats.
- **Incremental re-review** — `--since SHA` (and the webhook's `synchronize` `before` SHA) reviews only hunks
  changed since that commit; findings on a line the bot already commented on are dropped.
- **Prompt caching** — the shared PR context is a byte-identical leading block marked
  `cache_control: ephemeral`, with the specialist focus after it; optional cache warm-up (`CACHE_WARMUP`).
- **Usage tracking** — per-call input/output/cache tokens and latency, aggregated into `ReviewResult.usage` with
  an estimated USD cost from a configurable `MODEL_PRICES` table; one-line usage footer in the CLI and the
  GitHub summary.
- **More eval cases** — race condition (check-then-act), path traversal, N+1 query, untested branch,
  `None` dereference after a changed return, and a second clean refactor; a test checks every case is well-formed.
- **Suggestion blocks** — optional `replacement` on findings; single-line fixes render as a one-click GitHub
  ```` ```suggestion ```` block.
- **Feedback report** — `acr feedback owner/repo [--days 30]` turns 👍/👎 reactions on the bot's comments into
  precision per agent and per category.
- **Pluggable embedding retrieval** — `Retriever` protocol with the lexical retriever as default and an
  `EmbeddingRetriever` (chunk → injectable embedder → cosine top-k), selected with `RETRIEVER=lexical|embedding`.
- **Multi-language symbol detection** — changed symbols for JS/TS, Go, and Java/Kotlin feed related-snippet
  retrieval.
- `acr --version`.

[0.1.0]: https://github.com/Dhillon001/agentic-code-review/releases/tag/v0.1.0
