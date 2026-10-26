from __future__ import annotations

import pytest

from reviewer.config import Settings


class FakeLLM:
    """Scriptable stand-in for the model. `findings_by_agent` maps specialist name -> raw findings;
    `reject` is a set of (file, line) the verifier should reject."""

    def __init__(self, findings_by_agent: dict[str, list[dict]], reject: set[tuple[str, int]] = frozenset()):
        self.findings_by_agent = findings_by_agent
        self.reject = reject
        self.calls: list[str] = []
        self.systems: list[str] = []

    async def call_tool(self, *, model, system, user, tool, max_tokens=8000):
        self.calls.append(tool["name"])
        self.systems.append(system)
        if tool["name"] == "report_findings":
            agent = system.split("`")[1]  # "You are the `<name>` reviewer."
            return {"findings": self.findings_by_agent.get(agent, [])}
        # verifier: parse the listing back out of the prompt
        import json
        import re

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
