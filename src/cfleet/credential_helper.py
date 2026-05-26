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


def _server_url() -> str:
    return os.environ.get("CFLEET_SERVER_URL", "")


def _fleet_token() -> str:
    return os.environ.get("CFLEET_TOKEN", "")


def _worker_name() -> str:
    return os.environ.get("CFLEET_WORKER_NAME", "")


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
        print("Error: CFLEET_SERVER_URL and CFLEET_WORKER_NAME must be set", file=sys.stderr)
        sys.exit(1)

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
