"""Files not worth a reviewer's tokens: lockfiles, minified/generated code, vendored trees, binaries.

They're dropped before context building so they neither eat the `max_files` budget nor get read from the
source. `NOISE_PATTERNS` is the single default list; `Settings.ignore` / `.acr.yml` `ignore` extend it.
Pattern syntax is the same as `.acr.yml` (see `repo_config.path_ignored`).
"""

from __future__ import annotations

from reviewer.models import FileDiff
from reviewer.repo_config import path_ignored

NOISE_PATTERNS: list[str] = [
    # lockfiles
    "*.lock",  # poetry, uv, Cargo, yarn, Gemfile, composer, Pipfile, flake
    "*.lockb",
    "package-lock.json",
    "npm-shrinkwrap.json",
    "pnpm-lock.yaml",
    "go.sum",
    # minified / bundled
    "*.min.js",
    "*.min.css",
    "*.js.map",
    "*.css.map",
    # generated code
    "*_pb2.py",
    "*_pb2_grpc.py",
    "*_pb2.pyi",
    "*.pb.go",
    "*.generated.*",
    # build output and vendored dependencies
    "dist/",
    "vendor/",
    "node_modules/",
]


def is_noise(diff: FileDiff, extra: list[str] | None = None) -> bool:
    return diff.binary or path_ignored(diff.path, [*NOISE_PATTERNS, *(extra or [])])


def split_noise(diffs: list[FileDiff], extra: list[str] | None = None) -> tuple[list[FileDiff], list[str]]:
    """Returns (diffs to review, paths skipped)."""
    kept: list[FileDiff] = []
    skipped: list[str] = []
    for d in diffs:
        if is_noise(d, extra):
            skipped.append(d.path)
        else:
            kept.append(d)
    return kept, skipped
