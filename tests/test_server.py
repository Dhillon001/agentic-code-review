import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

import reviewer.server as server
from reviewer.config import Settings
from reviewer.webhook_queue import ReviewQueue


class _Client(TestClient):
    def post(self, *args, **kwargs):  # let queued reviews run before the test inspects them
        r = super().post(*args, **kwargs)
        self.portal.call(server.queue.drain)
        return r


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(server, "get_settings", lambda: Settings(_env_file=None, github_webhook_secret="s3cret"))
    queued = []

    async def fake_run(repo, number, since=None):
        queued.append((repo, number, since))

    monkeypatch.setattr(server, "queue", ReviewQueue(fake_run))
    with _Client(server.app) as c:  # one event loop across requests, so queued reviews can run
        yield c, queued


def _pr_event(action="opened", **extra) -> bytes:
    return json.dumps({"action": action, "pull_request": {"number": 7, "draft": False},
                       "repository": {"full_name": "o/r"}, **extra}).encode()


def _headers(body: bytes, delivery: str | None = None) -> dict:
    h = {"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": _sig(body)}
    if delivery:
        h["X-GitHub-Delivery"] = delivery
    return h


def _sig(body: bytes) -> str:
    return "sha256=" + hmac.new(b"s3cret", body, hashlib.sha256).hexdigest()


def test_rejects_bad_signature(client):
    c, _ = client
    r = c.post("/webhook", content=b"{}", headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": "sha256=bad"})
    assert r.status_code == 401


def test_queues_opened_pr(client):
    c, queued = client
    body = json.dumps({"action": "opened", "pull_request": {"number": 7, "draft": False},
                       "repository": {"full_name": "o/r"}}).encode()
    r = c.post("/webhook", content=body, headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": _sig(body)})
    assert r.json() == {"queued": True}
    assert queued == [("o/r", 7, None)]


def test_synchronize_reviews_only_new_commits(client):
    c, queued = client
    body = json.dumps({"action": "synchronize", "before": "abc123", "pull_request": {"number": 7, "draft": False},
                       "repository": {"full_name": "o/r"}}).encode()
    r = c.post("/webhook", content=body, headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": _sig(body)})
    assert r.json() == {"queued": True}
    assert queued == [("o/r", 7, "abc123")]


def test_ignores_drafts(client):
    c, queued = client
    body = json.dumps({"action": "opened", "pull_request": {"number": 7, "draft": True},
                       "repository": {"full_name": "o/r"}}).encode()
    r = c.post("/webhook", content=body, headers={"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": _sig(body)})
    assert "ignored" in r.json() and queued == []


def test_duplicate_delivery_is_handled_once(client):
    c, queued = client
    body = _pr_event()
    first = c.post("/webhook", content=body, headers=_headers(body, "d-1"))
    again = c.post("/webhook", content=body, headers=_headers(body, "d-1"))
    other = c.post("/webhook", content=body, headers=_headers(body, "d-2"))
    assert first.json() == {"queued": True}
    assert again.json() == {"duplicate": "d-1"}
    assert other.json() == {"queued": True}
    assert queued == [("o/r", 7, None), ("o/r", 7, None)]


def test_unsigned_request_does_not_poison_delivery_log(client):
    c, queued = client
    body = _pr_event()
    bad = c.post("/webhook", content=body, headers={**_headers(body, "d-1"), "X-Hub-Signature-256": "sha256=x"})
    assert bad.status_code == 401
    assert c.post("/webhook", content=body, headers=_headers(body, "d-1")).json() == {"queued": True}
    assert queued == [("o/r", 7, None)]


def test_deliveries_without_id_are_not_deduplicated(client):
    c, queued = client
    body = _pr_event()
    c.post("/webhook", content=body, headers=_headers(body))
    c.post("/webhook", content=body, headers=_headers(body))
    assert len(queued) == 2


def test_healthz_reports_queue_depth(client):
    c, _ = client
    assert c.get("/healthz").json() == {"ok": True, "queue_depth": 0}
