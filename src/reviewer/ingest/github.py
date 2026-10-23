"""Thin async GitHub REST client — only the endpoints the reviewer needs."""

from __future__ import annotations

import httpx

API = "https://api.github.com"


class GitHubClient:
    def __init__(self, token: str, base_url: str = API):
        self._http = httpx.AsyncClient(
            base_url=base_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=30,
        )

    async def __aenter__(self) -> GitHubClient:
        return self

    async def __aexit__(self, *exc) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        await self._http.aclose()

    async def get_pr(self, repo: str, number: int) -> dict:
        r = await self._http.get(f"/repos/{repo}/pulls/{number}")
        r.raise_for_status()
        return r.json()

    async def get_pr_diff(self, repo: str, number: int) -> str:
        r = await self._http.get(
            f"/repos/{repo}/pulls/{number}",
            headers={"Accept": "application/vnd.github.diff"},
        )
        r.raise_for_status()
        return r.text

    async def get_file(self, repo: str, path: str, ref: str) -> str | None:
        r = await self._http.get(
            f"/repos/{repo}/contents/{path}",
            params={"ref": ref},
            headers={"Accept": "application/vnd.github.raw+json"},
        )
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.text

    async def search_code(self, query: str, repo: str, limit: int = 5) -> list[str]:
        """Code search only indexes the default branch and is rate-limited (~10 req/min)."""
        r = await self._http.get(
            "/search/code", params={"q": f"{query} repo:{repo}", "per_page": limit}
        )
        if r.status_code in (403, 422, 429):  # rate limit / unindexed repo: degrade gracefully
            return []
        r.raise_for_status()
        return [item["path"] for item in r.json().get("items", [])]

    async def post_review(
        self, repo: str, number: int, commit_id: str, body: str, comments: list[dict]
    ) -> dict:
        r = await self._http.post(
            f"/repos/{repo}/pulls/{number}/reviews",
            json={"commit_id": commit_id, "body": body, "event": "COMMENT", "comments": comments},
        )
        r.raise_for_status()
        return r.json()
