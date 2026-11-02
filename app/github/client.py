from __future__ import annotations

import os
import shutil
import subprocess

import httpx


class GitHubClient:
    def __init__(self, *, timeout: int):
        self.timeout = timeout

    def has_gh_cli(self) -> bool:
        return shutil.which("gh") is not None

    def _get_branch_head_with_gh(self, *, owner: str, repo: str, branch: str, token: str | None) -> str:
        env = os.environ.copy()
        if token:
            env.setdefault("GH_TOKEN", token)

        completed = subprocess.run(
            ["gh", "api", f"repos/{owner}/{repo}/branches/{branch}", "--jq", ".commit.sha"],
            capture_output=True,
            check=True,
            text=True,
            timeout=self.timeout,
            env=env,
        )
        sha = completed.stdout.strip()
        if not sha:
            raise RuntimeError("gh api returned an empty commit SHA.")
        return sha

    def _get_branch_head_with_http(self, *, owner: str, repo: str, branch: str, token: str) -> str:
        response = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/branches/{branch}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        return payload["commit"]["sha"]

    def get_branch_head(self, *, owner: str, repo: str, branch: str, token: str | None) -> str:
        if self.has_gh_cli():
            try:
                return self._get_branch_head_with_gh(owner=owner, repo=repo, branch=branch, token=token)
            except Exception:
                if not token:
                    raise
        if not token:
            raise RuntimeError("A GitHub PAT is required when gh is unavailable or not authenticated.")
        return self._get_branch_head_with_http(owner=owner, repo=repo, branch=branch, token=token)

    def _compare_revisions_with_gh(self, *, owner: str, repo: str, base: str, head: str, token: str | None) -> str:
        env = os.environ.copy()
        if token:
            env.setdefault("GH_TOKEN", token)

        completed = subprocess.run(
            ["gh", "api", f"repos/{owner}/{repo}/compare/{base}...{head}", "--jq", ".status"],
            capture_output=True,
            check=True,
            text=True,
            timeout=self.timeout,
            env=env,
        )
        status = completed.stdout.strip()
        if not status:
            raise RuntimeError("gh api returned an empty compare status.")
        return status

    def _compare_revisions_with_http(self, *, owner: str, repo: str, base: str, head: str, token: str) -> str:
        response = httpx.get(
            f"https://api.github.com/repos/{owner}/{repo}/compare/{base}...{head}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        return str(payload["status"])

    def compare_revisions(self, *, owner: str, repo: str, base: str, head: str, token: str | None) -> str:
        if self.has_gh_cli():
            try:
                return self._compare_revisions_with_gh(owner=owner, repo=repo, base=base, head=head, token=token)
            except Exception:
                if not token:
                    raise
        if not token:
            raise RuntimeError("A GitHub PAT is required when gh is unavailable or not authenticated.")
        return self._compare_revisions_with_http(owner=owner, repo=repo, base=base, head=head, token=token)
