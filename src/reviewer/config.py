from __future__ import annotations

import logging
import re
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from reviewer.models import Severity
from reviewer.usage import DEFAULT_PRICES, ModelPrice

log = logging.getLogger(__name__)

AGENT_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,39}$")
MAX_FOCUS_CHARS = 1000  # a custom specialist's focus is prompt text; keep it short
# model ids as providers spell them (`claude-sonnet-5-5`, `claude-haiku-4-5@20251001`, `org/model:tag`)
MODEL_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@-]{0,99}$")
MAX_AGENT_MODELS = 32


def normalize_agent_models(v: object, origin: str = "agent models") -> dict[str, str]:
    """`{specialist name: model}`. Names are lowercased; an entry with a bad name or model id is skipped
    with a warning (the rest still apply) and that agent falls back to `reviewer_model`."""
    if v is None:
        return {}
    if not isinstance(v, dict):
        raise ValueError("must map agent name -> model")
    out: dict[str, str] = {}
    for raw_name, raw_model in v.items():
        name = str(raw_name or "").strip().lower()
        model = str(raw_model or "").strip() if isinstance(raw_model, (str, int, float)) else ""
        if not AGENT_NAME_RE.match(name):
            log.warning("%s: ignoring entry %r: not a valid agent name", origin, raw_name)
            continue
        if not MODEL_NAME_RE.match(model):
            log.warning("%s: ignoring %r: %r is not a valid model id", origin, name, raw_model)
            continue
        out[name] = model
    if len(out) > MAX_AGENT_MODELS:
        log.warning("%s: only the first %d entries are used", origin, MAX_AGENT_MODELS)
    return dict(list(out.items())[:MAX_AGENT_MODELS])


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
    # Per-specialist override of `reviewer_model`, e.g. {"security": "claude-opus-5-5", "tests": "claude-haiku-4-5"}
    # (env AGENT_MODELS as JSON; `.acr.yml` `models:` overrides per name). Unlisted agents use `reviewer_model`.
    agent_models: dict[str, str] = Field(default_factory=dict)
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

    @field_validator("agent_models", mode="before")
    @classmethod
    def _agent_models(cls, v: object) -> dict[str, str]:
        return normalize_agent_models(v, "AGENT_MODELS")

    def model_for(self, agent: str) -> str:
        """The model a specialist runs on: its `agent_models` entry, else `reviewer_model`."""
        return self.agent_models.get(agent) or self.reviewer_model


@lru_cache
def get_settings() -> Settings:
    return Settings()
