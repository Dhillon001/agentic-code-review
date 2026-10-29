from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from reviewer.usage import Usage


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"

    @property
    def rank(self) -> int:
        return {"critical": 3, "high": 2, "medium": 1, "low": 0}[self.value]


# ---------------------------------------------------------------- diff model


class Hunk(BaseModel):
    header: str
    new_start: int
    lines: list[str] = Field(default_factory=list)  # raw diff lines (with +/-/space prefix)
    added_lines: list[int] = Field(default_factory=list)  # new-file line numbers
    context_lines: list[int] = Field(default_factory=list)

    @property
    def commentable_lines(self) -> set[int]:
        """GitHub only accepts inline comments on lines that appear in the diff (RIGHT side)."""
        return set(self.added_lines) | set(self.context_lines)

    def render_numbered(self) -> str:
        """Diff with new-file line numbers in a gutter, so the model can cite exact lines."""
        out = [self.header]
        n = self.new_start
        for line in self.lines:
            if line.startswith("+"):
                out.append(f"{n:>5} + {line[1:]}")
                n += 1
            elif line.startswith("-"):
                out.append(f"{'':>5} - {line[1:]}")
            elif line.startswith("\\"):
                continue
            else:
                out.append(f"{n:>5}   {line[1:] if line.startswith(' ') else line}")
                n += 1
        return "\n".join(out)


class FileDiff(BaseModel):
    path: str
    old_path: str | None = None
    status: Literal["added", "modified", "deleted", "renamed"] = "modified"
    hunks: list[Hunk] = Field(default_factory=list)
    binary: bool = False  # "Binary files ... differ" / "GIT binary patch": nothing to review

    @property
    def commentable_lines(self) -> set[int]:
        return set().union(*(h.commentable_lines for h in self.hunks)) if self.hunks else set()

    @property
    def added_lines(self) -> set[int]:
        return {n for h in self.hunks for n in h.added_lines}

    def render_numbered(self) -> str:
        return "\n".join(h.render_numbered() for h in self.hunks)


# ---------------------------------------------------------------- context


class RelatedSnippet(BaseModel):
    path: str
    reason: str
    start_line: int
    text: str


class FileContext(BaseModel):
    diff: FileDiff
    content: str | None = None  # full post-change file content
    related: list[RelatedSnippet] = Field(default_factory=list)

    @property
    def path(self) -> str:
        return self.diff.path


class PRContext(BaseModel):
    repo: str
    number: int | None = None
    title: str = ""
    body: str = ""
    head_sha: str | None = None
    since_sha: str | None = None  # incremental re-review: only hunks changed since this commit
    files: list[FileContext] = Field(default_factory=list)
    skipped: list[str] = Field(default_factory=list)  # paths dropped as noise or ignored before review


# ---------------------------------------------------------------- findings


class Finding(BaseModel):
    agent: str = ""
    file: str
    line: int
    severity: Severity
    category: str
    title: str
    detail: str
    suggestion: str | None = None
    replacement: str | None = None  # exact new content for `line`; rendered as a GitHub suggestion block
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    verified: bool = False
    verifier_note: str | None = None

    @field_validator("replacement")
    @classmethod
    def _normalise_replacement(cls, v: str | None) -> str | None:
        """Drop a trailing newline; treat an empty replacement as "no replacement" (never a silent delete)."""
        if v is None:
            return None
        v = v.rstrip("\r\n")
        return v if v.strip() else None

    @property
    def single_line_replacement(self) -> str | None:
        """The replacement, if it is one line and can safely become a one-click suggestion."""
        return self.replacement if self.replacement is not None and "\n" not in self.replacement else None


class ReviewResult(BaseModel):
    inline: list[Finding] = Field(default_factory=list)  # posted as inline comments
    summary_only: list[Finding] = Field(default_factory=list)  # real issue, but off-diff
    rejected: list[Finding] = Field(default_factory=list)  # killed by verifier (kept for evals)
    stats: dict[str, int] = Field(default_factory=dict)
    usage: Usage = Field(default_factory=Usage)  # tokens, estimated cost, and wall time for this review
