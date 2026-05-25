"""GitHub App token broker — generates scoped installation tokens server-side.

Workers request tokens from the fleet server. The server enforces per-worker
permission policies and generates short-lived installation tokens using the
GitHub App private key (which never leaves the server).
"""

from __future__ import annotations

import time

import httpx
import jwt

from cfleet.config import (
    FleetConfig,
    FleetState,
    GH_PERMISSION_MAP,
    GitHubLevel,
    GitHubTokenLog,
)

GITHUB_API = "https://api.github.com"
_GH_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}


class GitHubTokenError(Exception):
    pass


def _make_app_jwt(config: FleetConfig) -> str:
    path = config.github.resolve_private_key_path()
    if not path.exists():
        raise GitHubTokenError(f"Private key not found at {path}. Run 'cfleet gh setup'.")
    now = int(time.time())
    return jwt.encode(
        {"iat": now - 60, "exp": now + 600, "iss": config.github.app_id},
        path.read_text(),
        algorithm="RS256",
    )


def _request_installation_token(config: FleetConfig, permissions: dict, repos: list[str] | None = None) -> dict:
    app_jwt = _make_app_jwt(config)
    body: dict = {"permissions": permissions}
    if repos:
        body["repositories"] = repos

    resp = httpx.post(
        f"{GITHUB_API}/app/installations/{config.github.installation_id}/access_tokens",
        headers={"Authorization": f"Bearer {app_jwt}", **_GH_HEADERS},
        json=body,
        timeout=15,
    )
    if resp.status_code != 201:
        detail = resp.json().get("message", resp.text)
        raise GitHubTokenError(f"GitHub API error ({resp.status_code}): {detail}")
    return resp.json()


def generate_installation_token(config: FleetConfig, worker_name: str, state: FleetState) -> dict:
    """Generate a scoped GitHub installation token for a worker.

    Validates the worker's configured level, requests a scoped token from
    GitHub, appends to the audit log, and returns the token + metadata.
    """
    if not config.github.is_configured():
        raise GitHubTokenError("GitHub App not configured. Run 'cfleet gh setup'.")
    if worker_name not in state.workers:
        raise GitHubTokenError(f"Worker '{worker_name}' not found.")

    worker = state.workers[worker_name]
    level = GitHubLevel(worker.github_level)
    if level == GitHubLevel.NONE:
        raise GitHubTokenError(
            f"Worker '{worker_name}' has github_level=none. "
            f"Set it with: cfleet gh set {worker_name} read|triage|write"
        )

    permissions = GH_PERMISSION_MAP[level]
    data = _request_installation_token(config, permissions, worker.repos or None)

    state.github_token_log.append(GitHubTokenLog(
        worker_name=worker_name,
        level=level.value,
        repos=worker.repos,
        expires_at=data.get("expires_at", ""),
    ))
    if len(state.github_token_log) > 500:
        state.github_token_log = state.github_token_log[-500:]
    state.save()

    return {
        "token": data["token"],
        "expires_at": data.get("expires_at", ""),
        "permissions": permissions,
        "level": level.value,
    }


def warn_unprotected_repos(config: FleetConfig, repos: list[str]) -> list[str]:
    """Check repos for missing branch protection. Returns list of warning strings."""
    if not config.github.is_configured() or not repos:
        return []

    try:
        token_data = _request_installation_token(
            config, {"metadata": "read", "administration": "read"},
        )
    except GitHubTokenError:
        return []

    headers = {"Authorization": f"Bearer {token_data['token']}", **_GH_HEADERS}
    warnings = []
    client = httpx.Client(timeout=15)
    try:
        for repo_name in repos:
            repo_resp = client.get(f"{GITHUB_API}/repos/{repo_name}", headers=headers)
            if repo_resp.status_code != 200:
                warnings.append(f"{repo_name}: could not access repo")
                continue
            branch = repo_resp.json().get("default_branch", "main")
            bp_resp = client.get(
                f"{GITHUB_API}/repos/{repo_name}/branches/{branch}/protection",
                headers=headers,
            )
            if bp_resp.status_code != 200:
                warnings.append(f"{repo_name}: no branch protection on {branch}")
    finally:
        client.close()

    return warnings
