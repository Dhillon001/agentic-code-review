import hashlib
import hmac
import json

from fastapi.testclient import TestClient

import reviewer.server as server
from reviewer.config import Settings


def _client(monkeypatch):
    monkeypatch.setattr(server, "get_settings", lambda: Settings(_env_file=None, github_webhook_secret="s3cret"))
    queued = []

    async def fake_run(repo, number):
        queued.append((repo, number))

    monkeypatch.setattr(server, "_run", fake_run)
    return TestClient(server.app), queued


def _sig(body: bytes) -> str:
    return "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()


def test_rejects_bad_signature(monkeypatch):
    c, _ = _client(monkeypatch)
    r = c.post("/webhook", content=b"{}", headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": "sha256=bad"})
    assert r.status_code == 401


def test_queues_opened_pr(monkeypatch):
    c, queued = _client(monkeypatch)
    body = json.dumps({"action": "opened", "pull_request": {"number": 7, "draft": False},
                       "repository": {"full_name": "o/r"}}).encode()
    r = c.post("/webhook", content=body, headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": _sig(body)})
    assert r.json() == {"queued": True}
    assert queued == [("o/r", 7)]


def test_ignores_drafts(monkeypatch):
    c, queued = _client(monkeypatch)
    body = json.dumps({"action": "opened", "pull_request": {"number": 7, "draft": True},
                       "repository": {"full_name": "o/r"}}).encode()
    r = c.post("/webhook", content=body, headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": _sig(body)})
    assert "ignored" in r.json() and queued == []
