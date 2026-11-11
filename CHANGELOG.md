# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added

- **CI gate** — `acr pr|local --fail-on {low,medium,high,critical}` exits 1 when any posted finding is at or
  above that severity, 2 when the review itself fails (bad config, missing key, API error), and 0 otherwise.
  Verifier-rejected findings never fail a build.
- **Inline suppressions** — `acr: ignore` (same line), `acr: ignore-next-line`, and scoped
  `acr: ignore[security, n-plus-one]` comments in any language silence matching findings. Scopes match an agent
  name or category. Suppressed findings skip the verifier and are counted as `suppressed` in stats.
- **Markdown report** — `acr pr|local --format {text,json,markdown}` (`--json` kept as an alias) and
  `--out PATH`. Markdown groups findings by file (most severe first) with severity icons, `suggestion` blocks,
  off-diff markers, and the funnel/usage footer — handy for `$GITHUB_STEP_SUMMARY`.
- **SARIF output** — `--format sarif` emits SARIF 2.1.0 for `github/codeql-action/upload-sarif`: one rule per
  category (security categories tagged with `security-severity`), `error`/`warning`/`note` levels from severity,
  repo-relative locations, line-independent partial fingerprints, and single-line replacements as SARIF fixes.
- **Secret scanning pre-pass** — deterministic detectors for AWS access keys, GitHub/Slack/Stripe tokens, Slack
  webhooks, private-key headers, and high-entropy `password=`/`api_key=`-style assignments run on added lines
  before any model call. Hits are `critical` `secrets` findings (confidence 1.0) that skip the verifier; values
  are redacted in all output, including agent findings that quote them. `SECRET_SCAN=false` disables it.
- **Per-path rules** — `.acr.yml` `paths:` (or the `PATH_RULES` setting) maps globs to extra team rules, e.g.
  `"src/api/**": Validate input with pydantic`. Rules reach only the prompts of batches containing a matching
  file (and that file's verifier call), so unrelated code isn't judged against them; per-batch system prompts
  stay byte-identical across specialists, so prompt caching still hits.
- **Custom specialists** — `.acr.yml` `agents:` (or `CUSTOM_AGENTS`) adds reviewers with a `name`, `focus`,
  and optional `severity_floor` (that agent's own minimum severity, held even if the verifier downgrades);
  `disable: [performance]` (or `DISABLE_AGENTS`) switches built-in or custom agents off. Names are validated,
  can't shadow built-ins, and bad entries or unknown disables are skipped with a warning.
- **Per-agent model routing** — `AGENT_MODELS` (JSON) and `.acr.yml` `models:` map a specialist name to a
  model, e.g. Opus for `security` and Haiku for `tests`; unlisted agents fall back to `REVIEWER_MODEL`. Usage
  and cost estimates are tracked per model, cache warm-up runs once per model, and unknown agent names or
  invalid model ids are ignored with a warning.
- **Language hints** — specialists get a short pitfalls checklist for each language in their batch, detected
  by file extension (Python async/mutable defaults, JS/TS floating promises and `==`, Go ignored errors and
  goroutine leaks, Java nulls and unclosed resources, SQL injection in raw queries). The hints live in one
  table in `agents/languages.py`, are scoped per batch so prompt caching still hits, and `LANGUAGE_HINTS=false`
  turns them off.
- **Resilient agent calls** — every model call gets a per-attempt timeout (`LLM_TIMEOUT_S`) and up to
  `LLM_MAX_RETRIES` retries with exponential backoff + full jitter on transient errors (rate limit, overload,
  5xx, timeout, dropped connection), honouring `retry-after`; other errors fail fast. Agents that still fail are
  listed in `ReviewResult.failed_agents` and flagged as a "partial review" in the GitHub summary, Markdown and
  text reports; a failed verifier call holds that file's findings back. One failing agent never crashes a review.

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
