"""Per-repo config: an optional `.acr.yml` at the repo root, merged over `Settings`.

    # .acr.yml
    ignore: ["docs/**", "*.snap", "migrations/"]
    min_severity: high
    max_comments: 5
    rules: |
      - All DB access goes through the repository layer, never raw SQL in handlers.
      - Public functions in `api/` must validate input with pydantic models.

A broken file never breaks a review: bad YAML is ignored and bad fields fall back to defaults.
"""

from __future__ import annotations

import logging
from fnmatch import fnmatchcase

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from reviewer.config import Settings
from reviewer.ingest.sources import Source
from reviewer.models import FileDiff, Severity

log = logging.getLogger(__name__)

CONFIG_PATH = ".acr.yml"
MAX_RULES_CHARS = 4000  # keep a runaway rules block from eating the prompt budget


class RepoConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ignore: list[str] = Field(default_factory=list)
    min_severity: Severity | None = None
    max_comments: int | None = Field(default=None, ge=0)
    rules: str = ""

    @field_validator("ignore", mode="before")
    @classmethod
    def _ignore_list(cls, v: object) -> object:
        if v is None:
            return []
        return [v] if isinstance(v, str) else v

    @field_validator("rules", mode="before")
    @classmethod
    def _rules_text(cls, v: object) -> object:
        if v is None:
            return ""
        if isinstance(v, list):
            return "\n".join(f"- {str(r).strip()}" for r in v if str(r).strip())
        return v

    @field_validator("rules")
    @classmethod
    def _rules_cap(cls, v: str) -> str:
        return v.strip()[:MAX_RULES_CHARS]


def parse_repo_config(text: str | None) -> RepoConfig:
    if not text or not text.strip():
        return RepoConfig()
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        log.warning("ignoring %s: invalid YAML (%s)", CONFIG_PATH, e)
        return RepoConfig()
    if data is None:
        return RepoConfig()
    if not isinstance(data, dict):
        log.warning("ignoring %s: top level must be a mapping", CONFIG_PATH)
        return RepoConfig()
    try:
        return RepoConfig.model_validate(data)
    except ValidationError as e:
        bad = {err["loc"][0] for err in e.errors() if err["loc"]}
        log.warning("%s: ignoring invalid fields %s", CONFIG_PATH, sorted(map(str, bad)))
        return RepoConfig.model_validate({k: v for k, v in data.items() if k not in bad})


async def load_repo_config(source: Source) -> RepoConfig:
    return parse_repo_config(await source.read_file(CONFIG_PATH))


def apply_repo_config(settings: Settings, cfg: RepoConfig) -> Settings:
    """Repo values win over global ones; `ignore` extends rather than replaces."""
    update: dict = {"ignore": [*settings.ignore, *cfg.ignore]}
    if cfg.min_severity is not None:
        update["min_severity"] = cfg.min_severity.value
    if cfg.max_comments is not None:
        update["max_comments"] = cfg.max_comments
    if cfg.rules:
        update["rules"] = cfg.rules
    return settings.model_copy(update=update)


def path_ignored(path: str, patterns: list[str]) -> bool:
    """`dir/` matches that directory anywhere in the path; patterns without a `/` also match the
    basename (`*.snap`); anything else is an fnmatch glob on the full path (`*` crosses `/`)."""
    parts = path.split("/")
    for pat in patterns:
        pat = pat.strip().removeprefix("./")
        if not pat:
            continue
        if pat.endswith("/"):
            d = pat.rstrip("/")
            if "/" in d:
                if path.startswith(d + "/"):
                    return True
            elif d in parts[:-1]:
                return True
        elif fnmatchcase(path, pat) or ("/" not in pat and fnmatchcase(parts[-1], pat)):
            return True
    return False


def filter_ignored(diffs: list[FileDiff], patterns: list[str]) -> list[FileDiff]:
    if not patterns:
        return diffs
    return [d for d in diffs if not path_ignored(d.path, patterns)]
