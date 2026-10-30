# Roadmap — v0.1.0

One step per commit. Every step ships with tests (offline, via `tests/conftest.py::FakeLLM` — no real API calls)
and keeps `pytest -q` green. Tick the box in the same commit that implements the step.

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
- [ ] **Multi-language symbols.** Improve `changed_symbols` for JS/TS (`function`, `const x = (…) =>`, class methods,
  `export default function`), Go (`func (r *T) Name`), and Java/Kotlin methods. Table-driven tests per language.

## Sat Oct 31

- [ ] **Release v0.1.0.** Add `CHANGELOG.md` summarising everything above, bump version to 0.1.0 if needed,
  add README badges (Python version, license, tests), refresh README feature list, and create tag `v0.1.0`.
