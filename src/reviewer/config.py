from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # Pipeline behaviour
    verify: bool = True
    min_confidence: float = 0.5  # drop raw findings below this before verification
    min_severity: str = "medium"  # low | medium | high | critical
    max_comments: int = 10  # hard cap on comments posted per PR
    ignore: list[str] = Field(default_factory=list)  # path globs never reviewed (.acr.yml extends)
    rules: str = ""  # team conventions injected into reviewer prompts (usually from .acr.yml)

    # Context building
    max_files: int = 50
    full_file_max_lines: int = 400  # send whole file if shorter, else windows around hunks
    context_lines: int = 30
    max_related_per_file: int = 3
    max_prompt_chars: int = 120_000  # per specialist call; files are batched to fit


@lru_cache
def get_settings() -> Settings:
    return Settings()
