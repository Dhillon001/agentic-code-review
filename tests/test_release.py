"""Release hygiene: the version is stated once per place and they all agree."""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

from reviewer import __version__
from reviewer import cli

ROOT = Path(__file__).resolve().parent.parent


def _pyproject_version() -> str:
    match = re.search(r'^version\s*=\s*"([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.MULTILINE)
    assert match, "no version in pyproject.toml"
    return match.group(1)


def test_package_version_matches_pyproject():
    assert __version__ == _pyproject_version()


def test_changelog_has_entry_for_current_version():
    changelog = (ROOT / "CHANGELOG.md").read_text()
    headings = re.findall(r"^## \[([^\]]+)\]", changelog, re.MULTILINE)
    assert headings, "CHANGELOG.md has no version headings"
    assert headings[0] == __version__, "latest CHANGELOG entry should be the current version"
    assert f"[{__version__}]: " in changelog, "missing link reference for the current version"


def test_cli_version_flag(capsys, monkeypatch):
    monkeypatch.setattr(sys, "argv", ["acr", "--version"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 0
    assert capsys.readouterr().out.strip() == f"acr {__version__}"
