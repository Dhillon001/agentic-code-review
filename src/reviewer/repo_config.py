"""Per-repo config: an optional `.acr.yml` at the repo root, merged over `Settings`.

    # .acr.yml
    ignore: ["docs/**", "*.snap", "migrations/"]
    min_severity: high
    max_comments: 5
    rules: |
      - All DB access goes through the repository layer, never raw SQL in handlers.
    paths:                       # extra rules only for batches/files matching the glob
      "src/api/**": Validate input with pydantic models.
      "*.sql": [Every migration is reversible, No SELECT *]

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
MAX_PATH_RULES = 50  # cap on `paths:` entries

PATH_RULES_HEADER = "Rules for specific paths (each applies only to files matching its pattern):"


def _rules_text(v: object) -> object:
    """A rules value may be a string or a list of strings (rendered as a bullet list)."""
    if v is None:
        return ""
    if isinstance(v, list):
        return "\n".join(f"- {str(r).strip()}" for r in v if str(r).strip())
    return v


class RepoConfig(BaseModel):
    model_config = ConfigDict(extra="ignore")

    ignore: list[str] = Field(default_factory=list)
    min_severity: Severity | None = None
    max_comments: int | None = Field(default=None, ge=0)
    rules: str = ""
    paths: dict[str, str] = Field(default_factory=dict)

    @field_validator("ignore", mode="before")
    @classmethod
    def _ignore_list(cls, v: object) -> object:
        if v is None:
            return []
        return [v] if isinstance(v, str) else v

    @field_validator("rules", mode="before")
    @classmethod
    def _rules_text(cls, v: object) -> object:
        return _rules_text(v)

    @field_validator("rules")
    @classmethod
    def _rules_cap(cls, v: str) -> str:
        return v.strip()[:MAX_RULES_CHARS]

    @field_validator("paths", mode="before")
    @classmethod
    def _paths_map(cls, v: object) -> object:
        if v is None:
            return {}
        if not isinstance(v, dict):
            raise ValueError("paths must map glob -> rules")
        out: dict[str, object] = {}
        for glob, rules in v.items():
            text = _rules_text(rules)
            if not isinstance(text, str):
                raise ValueError(f"rules for {glob!r} must be a string or a list of strings")
            glob, text = str(glob).strip().removeprefix("./"), text.strip()[:MAX_RULES_CHARS]
            if glob and text:
                out[glob] = text
        if len(out) > MAX_PATH_RULES:
            log.warning("%s: only the first %d `paths` entries are used", CONFIG_PATH, MAX_PATH_RULES)
        return dict(list(out.items())[:MAX_PATH_RULES])


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
    if cfg.paths:
        update["path_rules"] = {**settings.path_rules, **cfg.paths}
    return settings.model_copy(update=update)


def path_matches(path: str, patterns: list[str]) -> bool:
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


path_ignored = path_matches  # historical name, used for `ignore`


def rules_for_paths(rules: str, path_rules: dict[str, str], paths: list[str]) -> str:
    """Global `rules` plus every `paths:` entry whose glob matches at least one of `paths`.

    Deterministic for a given set of paths, so every specialist reviewing one batch gets a byte-identical
    system prompt (prompt caching still hits)."""
    sections = [rules.strip()] if rules.strip() else []
    scoped = []
    for glob, text in path_rules.items():
        hits = [p for p in paths if path_matches(p, [glob])]
        if hits:
            scoped.append(f"`{glob}` (applies to: {', '.join(hits)}):\n{text.strip()}")
    if scoped:
        sections.append("\n".join([PATH_RULES_HEADER, *scoped]))
    return "\n\n".join(sections)


def filter_ignored(diffs: list[FileDiff], patterns: list[str]) -> list[FileDiff]:
    if not patterns:
        return diffs
    return [d for d in diffs if not path_ignored(d.path, patterns)]
