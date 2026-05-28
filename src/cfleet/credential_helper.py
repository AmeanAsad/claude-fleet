#!/usr/bin/env python3
"""Git credential helper that fetches GitHub tokens from the fleet server.

Deployed to worker machines during provisioning. Called by git as:
    git credential-cfleet get

Also usable directly:
    cfleet-gh-token            # prints token to stdout
    cfleet-gh-token --for-gh   # sets up gh CLI auth

The helper caches tokens and auto-renews on expiry. The private key
never leaves the server — this script only talks to the fleet server API.
"""

import json
import os
import sys
import time
from pathlib import Path

CACHE_PATH = Path.home() / ".cfleet-gh-token-cache.json"
RENEW_BUFFER_SECONDS = 300


def _load_config_fallback() -> dict:
    """Read server URL + token from ~/.cfleet/config.yml when env vars are absent.

    The agent's env wiring (cli.py) usually provides these, but if a subprocess
    chain loses the env (e.g. an SDK transport resets it), falling back to the
    on-disk config keeps the helper functional. Cached so repeated calls in one
    process don't re-read the file.
    """
    if not hasattr(_load_config_fallback, "_cache"):
        cfg = {"server_url": "", "token": ""}
        config_path = Path.home() / ".cfleet" / "config.yml"
        if config_path.exists():
            try:
                import yaml  # type: ignore
                data = yaml.safe_load(config_path.read_text()) or {}
                server = (data.get("server") or {})
                cfg["server_url"] = str(server.get("url") or "")
                # Prefer joiner_token (per-worker auth); fall back to operator token.
                cfg["token"] = str(server.get("joiner_token") or server.get("token") or "")
            except Exception:
                pass
        _load_config_fallback._cache = cfg  # type: ignore[attr-defined]
    return _load_config_fallback._cache  # type: ignore[attr-defined]


def _server_url() -> str:
    return os.environ.get("CFLEET_SERVER_URL", "") or _load_config_fallback()["server_url"]


def _fleet_token() -> str:
    return os.environ.get("CFLEET_TOKEN", "") or _load_config_fallback()["token"]


def _worker_name() -> str:
    """Worker name from env, else inferred from cwd.

    Agent workspaces are always provisioned at $HOME/<worker_name>/, so the
    leading path component under $HOME is the worker name when the helper is
    invoked by git inside that tree (the common case).
    """
    name = os.environ.get("CFLEET_WORKER_NAME", "")
    if name:
        return name
    try:
        cwd = Path.cwd().resolve()
        home = Path.home().resolve()
        rel = cwd.relative_to(home)
        if rel.parts:
            return rel.parts[0]
    except Exception:
        pass
    return ""


def _read_cache() -> dict | None:
    if not CACHE_PATH.exists():
        return None
    try:
        data = json.loads(CACHE_PATH.read_text())
        expires_at = data.get("expires_at", "")
        if not expires_at:
            return None
        from datetime import datetime, timezone
        expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
        remaining = (expiry - datetime.now(timezone.utc)).total_seconds()
        if remaining < RENEW_BUFFER_SECONDS:
            return None
        return data
    except Exception:
        return None


def _write_cache(data: dict) -> None:
    CACHE_PATH.write_text(json.dumps(data))
    CACHE_PATH.chmod(0o600)


def _fetch_token() -> dict:
    import urllib.request
    import urllib.error

    server = _server_url()
    token = _fleet_token()
    worker = _worker_name()

    if not server or not worker:
        # Helper is invoked outside a worker context (e.g. user's everyday git).
        # Exit silently so git falls through to the next credential helper in
        # the chain (osxkeychain etc.) instead of prompting for a password.
        sys.exit(0)

    url = f"{server}/api/github/token"
    body = json.dumps({"worker_name": worker}).encode()
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        detail = e.read().decode() if e.fp else str(e)
        print(f"Error fetching token: {detail}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error connecting to fleet server: {e}", file=sys.stderr)
        sys.exit(1)


def get_token() -> str:
    cached = _read_cache()
    if cached:
        return cached["token"]

    data = _fetch_token()
    _write_cache(data)
    return data["token"]


def git_credential_helper() -> None:
    """Handle git credential protocol (get/store/erase)."""
    if len(sys.argv) < 2:
        print(get_token())
        return

    action = sys.argv[1]
    if action != "get":
        return

    input_lines = {}
    for line in sys.stdin:
        line = line.strip()
        if not line:
            break
        if "=" in line:
            k, _, v = line.partition("=")
            input_lines[k] = v

    host = input_lines.get("host", "")
    if "github.com" not in host:
        return

    token = get_token()
    print(f"protocol=https")
    print(f"host=github.com")
    print(f"username=x-access-token")
    print(f"password={token}")
    print()


def setup_gh_cli() -> None:
    """Print token for piping into gh auth login."""
    print(get_token())


def main() -> None:
    if "--for-gh" in sys.argv:
        setup_gh_cli()
    else:
        git_credential_helper()


if __name__ == "__main__":
    main()
