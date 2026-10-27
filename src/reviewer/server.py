"""GitHub webhook service.  Run:  uvicorn reviewer.server:app --port 8000"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import BackgroundTasks, FastAPI, Header, HTTPException, Request

from reviewer.config import get_settings
from reviewer.service import review_github_pr

log = logging.getLogger(__name__)
app = FastAPI(title="Agentic Code Review")

TRIGGER_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}


def signature_ok(secret: str, body: bytes, signature: str | None) -> bool:
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature or "")


async def _run(repo: str, number: int, since_sha: str | None = None) -> None:
    try:
        result = await review_github_pr(repo, number, get_settings(), post=True, since_sha=since_sha)
        log.info("reviewed %s#%s: %s", repo, number, result.stats)
    except Exception:  # background task: log, never crash the server
        log.exception("review failed for %s#%s", repo, number)


@app.get("/healthz")
async def healthz() -> dict:
    return {"ok": True}


@app.post("/webhook")
async def webhook(
    request: Request,
    background: BackgroundTasks,
    x_github_event: str = Header(default=""),
    x_hub_signature_256: str | None = Header(default=None),
) -> dict:
    settings = get_settings()
    if not settings.github_webhook_secret:
        raise HTTPException(500, "GITHUB_WEBHOOK_SECRET not configured")
    body = await request.body()
    if not signature_ok(settings.github_webhook_secret, body, x_hub_signature_256):
        raise HTTPException(401, "bad signature")

    if x_github_event != "pull_request":
        return {"ignored": f"event {x_github_event}"}
    payload = json.loads(body)
    pr = payload.get("pull_request", {})
    if payload.get("action") not in TRIGGER_ACTIONS or pr.get("draft"):
        return {"ignored": payload.get("action")}

    # On a push, `before` is the previous head: review only what the new commits touched.
    since = payload.get("before") if payload.get("action") == "synchronize" else None
    background.add_task(_run, payload["repository"]["full_name"], pr["number"], since)
    return {"queued": True}
