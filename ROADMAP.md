# Roadmap

One step per commit. Every step ships with tests (offline, via `tests/conftest.py::FakeLLM` and fake HTTP
transports — no real API calls) and keeps `pytest -q` green. Tick the box in the same commit that implements
the step. Update README/CHANGELOG `[Unreleased]` where a step adds user-facing behaviour.

# v0.2.0 — CI-ready, configurable, cheaper

## Sat Nov 7

- [x] **CI gate (`--fail-on`).** `acr pr|local --fail-on {low,medium,high,critical}` exits 1 when any posted
  finding is at or above that severity (exit 2 on internal error, 0 otherwise). Document in README for CI use.
- [x] **Inline suppressions.** A line containing `acr: ignore` (any comment syntax), or the line after one
  containing `acr: ignore-next-line`, suppresses findings on it; `acr: ignore[security]` scopes to a category.
  Suppressed findings are dropped after ranking and counted as `suppressed` in stats.

## Sun Nov 8

- [x] **Markdown report.** `--format {text,json,markdown}` (keep `--json` as an alias) plus `--out PATH`.
  Markdown groups findings by file with severity icons, suggestion blocks, and the usage footer.
- [x] **SARIF output.** `--format sarif` emits SARIF 2.1.0 (one rule per category, `level` from severity,
  physical locations, partial fingerprints) for GitHub code scanning upload. Validate structure in tests.

## Mon Nov 9

- [x] **Secret scanning pre-pass.** Deterministic regex detectors (AWS access keys, GitHub/Slack/Stripe tokens,
  private key headers, generic `password=`/`api_key=` assignments with high-entropy values) on added lines only.
  Emits `critical` `secrets` findings with `confidence=1.0` that skip the verifier; values are redacted in output.

## Tue Nov 10

- [x] **Per-path rules.** `.acr.yml` `paths:` maps globs to extra rules (`"src/api/**": "Validate input with
  pydantic"`); rules are injected only into prompts for batches containing matching files.
- [x] **Custom specialists.** `.acr.yml` `agents:` adds reviewers (`name`, `focus`, optional `severity_floor`)
  and `disable: [performance]` turns built-ins off. Validate names; unknown disables are ignored with a warning.
- [x] **Per-agent model routing.** `AGENT_MODELS` setting (and `.acr.yml` `models:`) maps specialist name →
  model, falling back to `reviewer_model`. Usage tracking and prices work per model.
- [x] **Language hints.** Detect each file's language from its extension and add a short, language-specific
  pitfalls list to the specialist prompt (Python async/mutable defaults, JS/TS promises/`==`, Go error handling
  and goroutine leaks, Java nulls/resources, SQL injection in raw queries). Hints live in one data table.

## Wed Nov 11

- [x] **Resilient agent calls.** Per-call timeout and retry with exponential backoff + jitter on transient
  errors (rate limit, overload, timeout). Agents that still fail are listed in `ReviewResult.failed_agents` and
  in the summary comment ("partial review"); the review never crashes on one agent.
- [ ] **Cost budget.** `MAX_COST_USD`: estimate input tokens (chars/4) and cost before calling; if over budget,
  drop lowest-priority files until it fits and report them as `skipped_budget` (and in the summary).
- [ ] **Risk-ranked files for large PRs.** When files exceed `max_files` or the budget, order by a risk score
  (security-sensitive paths like `auth`/`crypto`/`sql`, lines changed, new files, test files last). List
  unreviewed files in the summary.

## Thu Nov 12

- [ ] **Response cache.** `--cache-dir` / `LLM_CACHE_DIR`: a `CachingLLM` wrapper keyed by a hash of
  (model, system, user, tool schema) stores tool outputs as JSON, making eval reruns free and reproducible.
  Cache hits are reported in usage. Never caches errors.

## Fri Nov 13

- [ ] **Review a patch file.** `acr diff PATCH|- [--root DIR]` reviews any unified diff from a file or stdin,
  reading file contents from `--root` (default: cwd). Works without git or GitHub.

## Sat Nov 14

- [ ] **Pre-commit integration.** `acr staged` reviews `git diff --cached`; ship `.pre-commit-hooks.yaml` with
  an `acr` hook (`--fail-on high`) and README instructions.
- [ ] **Eval breakdown + baseline.** `run_evals.py` reports precision/recall per category and writes
  `--report-md`; `--baseline report.json` prints deltas and exits 1 if F1 drops by more than `--tolerance`.

## Sun Nov 15

- [ ] **Eval variance.** `run_evals.py --repeat N` runs each case N times and reports mean ± std for precision,
  recall and F1, plus per-case flakiness (cases whose outcome changed across runs).
- [ ] **Eval case scaffolder.** `evals/new_case.py --repo PATH --commit SHA --name NAME` builds a case directory
  from a local commit (diff + post-change files) with an `expected` stub to fill in; refuses to overwrite.

## Mon Nov 16

- [ ] **Updatable summary comment.** The summary is posted as an issue comment carrying a hidden
  `<!-- acr:summary -->` marker; re-reviews edit it in place instead of stacking new summaries.
- [ ] **Webhook hardening.** Deduplicate deliveries by `X-GitHub-Delivery`; per-PR latest-wins locking so a new
  push cancels an in-flight review of an older head; `/healthz` reports queue depth.
- [ ] **`/acr review` command.** `issue_comment` events on PRs from users with write access (check via the
  collaborator permission API) trigger a full re-review; `/acr help` replies with usage.
- [ ] **Release v0.2.0.** Move `[Unreleased]` into a `0.2.0` CHANGELOG section, bump version, refresh README
  features, and create tag `v0.2.0`.

# v0.1.0 — first release (done)

## Mon Oct 26

- [x] **Per-repo config (`.acr.yml`).** Load an optional `.acr.yml` from the repo root (via the `Source`) with
  `ignore` (glob list), `min_severity`, `max_comments`, and `rules` (free-text team conventions). Merge over
  `Settings`; inject `rules` into every specialist's system prompt. Add `pyyaml` dependency. Document in README.
- [x] **Skip noise files.** Before context building, drop lockfiles (`*.lock`, `package-lock.json`, `poetry.lock`, …),
  minified/generated files (`*.min.js`, `*_pb2.py`, `dist/`, `vendor/`, `node_modules/`), and binary diffs.
  Defaults live in one list; `.acr.yml` `ignore` extends it. Report skipped count in `ReviewResult.stats`.

## Tue Oct 27

- [x] **Incremental review.** New `GitHubClient.list_review_comments`; on re-review, (a) when a `since_sha` is given,
  only review hunks from `git compare since_sha...head`, and (b) drop findings on a (file, line) the bot already
  commented on. Wire `before` SHA from the `synchronize` webhook payload; add `--since` to `acr pr`.
- [x] **Prompt caching.** Restructure specialist calls so the shared PR context is an identical leading content
  block with `cache_control: {"type": "ephemeral"}` and the specialist focus comes after it. Keep the `LLM`
  protocol backward compatible (FakeLLM must still work). Note the cost rationale in README.

## Wed Oct 28

- [x] **Usage tracking.** `AnthropicLLM` records input/output/cache-read tokens and latency per call; the pipeline
  aggregates into `ReviewResult.usage` (tokens, est. USD via a per-model price table in config, wall time).
  Show a one-line usage footer in the GitHub summary and in CLI output.
- [x] **More eval cases.** Add six cases under `evals/cases/` in the existing format: race condition (check-then-act),
  path traversal, N+1 query in a loop, new branch without a test, None dereference after a changed return,
  and one more clean refactor. Add a test that every case loads and its expected lines are commentable.

## Thu Oct 29

- [x] **Suggestion blocks.** Add optional `replacement` (exact new line content) to the findings tool schema and
  `Finding`. When present and the finding is single-line, render a GitHub ```` ```suggestion ```` block in the inline
  comment instead of prose "Suggested fix".
- [x] **Feedback report.** `acr feedback owner/repo [--days 30]`: fetch the bot's review comments and their
  👍/👎 reactions, parse agent/category from the comment footer, and print precision per agent and per category.
  Pure functions for the aggregation, tested with fixture JSON.

## Fri Oct 30

- [x] **Pluggable embedding retrieval.** `Retriever` protocol with the current lexical implementation as default and
  an `EmbeddingRetriever` (chunk files, embed via an injectable `embed(texts) -> list[list[float]]`, cosine top-k).
  Selected by `retriever: lexical|embedding` setting. Tests use a deterministic fake embedder.
- [x] **Multi-language symbols.** Improve `changed_symbols` for JS/TS (`function`, `const x = (…) =>`, class methods,
  `export default function`), Go (`func (r *T) Name`), and Java/Kotlin methods. Table-driven tests per language.

## Sat Oct 31

- [x] **Release v0.1.0.** Add `CHANGELOG.md` summarising everything above, bump version to 0.1.0 if needed,
  add README badges (Python version, license, tests), refresh README feature list, and create tag `v0.1.0`.
