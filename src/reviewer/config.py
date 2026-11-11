from __future__ import annotations

import re
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from reviewer.models import Severity
from reviewer.usage import DEFAULT_PRICES, ModelPrice

AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
MAX_FOCUS_CHARS = 1000  # a custom specialist's focus is prompt text; keep it short


class CustomAgent(BaseModel):
    """An extra specialist reviewer, usually declared in `.acr.yml` `agents:`."""

    model_config = ConfigDict(extra="ignore")

    name: str
    focus: str
    # this agent's own minimum severity: its findings below it are dropped (before and after verification)
    severity_floor: Severity | None = None

    @field_validator("name", mode="before")
    @classmethod
    def _name(cls, v: object) -> str:
        name = str(v or "").strip().lower()
        if not AGENT_NAME_RE.match(name):
            raise ValueError(
                f"agent name {name!r} must be 1-40 chars of a-z, 0-9, '-' or '_', starting with a letter"
            )
        return name

    @field_validator("focus", mode="before")
    @classmethod
    def _focus(cls, v: object) -> str:
        focus = " ".join(str(v or "").split())
        if not focus:
            raise ValueError("agent focus must not be empty")
        return focus[:MAX_FOCUS_CHARS]


class Settings(BaseSettings):
    """All knobs live here. Override via env vars or a .env file (see .env.example)."""

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # Credentials
    anthropic_api_key: str = ""
    github_token: str = ""
    github_webhook_secret: str = ""

    # Models: cheaper/faster model fans out, stronger model verifies.
    reviewer_model: str = "claude-sonnet-5-5"
    verifier_model: str = "claude-opus-5-5"
    llm_concurrency: int = 4
    # Run one specialist per batch before the rest so the shared-context prompt cache is written
    # once and read by the others (parallel cold calls would each pay the full input price).
    cache_warmup: bool = True
    # USD per million tokens, keyed by model name or prefix (longest match wins). Used for the
    # estimated cost in the usage footer; override as JSON, e.g. {"claude-opus": {"input": 5, "output": 25}}.
    model_prices: dict[str, ModelPrice] = Field(default_factory=lambda: dict(DEFAULT_PRICES))

    # Pipeline behaviour
    verify: bool = True
    secret_scan: bool = True  # regex pre-pass for credentials on added lines (critical, skips the verifier)
    min_confidence: float = 0.5  # drop raw findings below this before verification
    min_severity: str = "medium"  # low | medium | high | critical
    max_comments: int = 10  # hard cap on comments posted per PR
    ignore: list[str] = Field(default_factory=list)  # path globs never reviewed (.acr.yml extends)
    rules: str = ""  # team conventions injected into reviewer prompts (usually from .acr.yml)
    # glob -> extra conventions, injected only into prompts whose batch/file matches (usually from .acr.yml `paths:`)
    path_rules: dict[str, str] = Field(default_factory=dict)
    # Specialists: extra reviewers alongside the built-ins, and names to switch off (usually from .acr.yml)
    custom_agents: list[CustomAgent] = Field(default_factory=list)
    disable_agents: list[str] = Field(default_factory=list)  # built-in or custom specialist names

    # Context building
    max_files: int = 50
    full_file_max_lines: int = 400  # send whole file if shorter, else windows around hunks
    context_lines: int = 30
    max_related_per_file: int = 3
    # How related snippets are found: `lexical` = call sites of changed symbols (default);
    # `embedding` = repo chunks ranked by embedding similarity (see reviewer/context/embedding.py).
    retriever: Literal["lexical", "embedding"] = "lexical"
    embedding_chunk_lines: int = 40
    embedding_max_files: int = 500  # files indexed per review when the source can list them
    max_prompt_chars: int = 120_000  # per specialist call; files are batched to fit


@lru_cache
def get_settings() -> Settings:
    return Settings()
