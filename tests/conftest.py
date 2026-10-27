from __future__ import annotations

import re

import pytest

from reviewer.agents.llm import as_text
from reviewer.config import Settings


class FakeLLM:
    """Scriptable stand-in for the model. `findings_by_agent` maps specialist name -> raw findings;
    `reject` is a set of (file, line) the verifier should reject."""

    def __init__(self, findings_by_agent: dict[str, list[dict]], reject: set[tuple[str, int]] = frozenset()):
        self.findings_by_agent = findings_by_agent
        self.reject = reject
        self.calls: list[str] = []
        self.systems: list[str] = []
        self.users: list = []  # raw `user` content as sent (str or content blocks)

    async def call_tool(self, *, model, system, user, tool, max_tokens=8000):
        self.calls.append(tool["name"])
        self.systems.append(system)
        self.users.append(user)
        user = as_text(user)
        if tool["name"] == "report_findings":
            agent = re.search(r"You are the `(\w+)` reviewer", user).group(1)
            return {"findings": self.findings_by_agent.get(agent, [])}
        # verifier: parse the listing back out of the prompt
        import json

        file = re.search(r"=== FILE: (.+?) ===", user).group(1)
        listing = json.loads(user.split("=== FINDINGS TO VERIFY ===\n", 1)[1])
        return {
            "verdicts": [
                {
                    "index": item["index"],
                    "verdict": "rejected" if (file, item["line"]) in self.reject else "confirmed",
                    "note": "fake",
                }
                for item in listing
            ]
        }


@pytest.fixture
def settings() -> Settings:
    return Settings(_env_file=None, anthropic_api_key="test", github_token="test")
