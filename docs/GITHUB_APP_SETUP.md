# GitHub App Setup for Claude Fleet

Claude Fleet uses a GitHub App to broker short-lived, scoped tokens to workers.
The app's private key stays on the fleet server — workers never see it. Each
worker gets a token scoped to its configured access level (read, triage, or write).

---

## 1. Create the GitHub App

Go to: **https://github.com/settings/apps/new**

(Or for an org: `https://github.com/organizations/<ORG>/settings/apps/new`)

### App name

Something like `claude-fleet-<yourname>` (must be globally unique on GitHub).

### Homepage URL

Anything — `https://github.com/AmeanAsad/claude-fleet` is fine.

### Webhook

- **Active**: uncheck (we don't need webhooks)

### Permissions

These are the **maximum** permissions the app can grant. Fleet will request
subsets based on each worker's level. Set all of these:

| Permission       | Access          | Used by level |
|------------------|-----------------|---------------|
| Contents         | Read and write  | read, write   |
| Metadata         | Read-only       | all           |
| Issues           | Read and write  | triage, write |
| Pull requests    | Read and write  | write         |
| Administration   | Read-only       | (branch protection checks) |

Leave everything else as "No access".

### Where can this app be installed?

- **Only on this account** (recommended, unless you need it on an org)

### Create the app

Click **Create GitHub App**.

---

## 2. Note the App ID

After creation, you'll be on the app's settings page.

Copy the **App ID** (a number like `123456`) — you'll need it for `cfleet gh setup`.

---

## 3. Generate a private key

On the same settings page, scroll to **Private keys** and click **Generate a private key**.

This downloads a `.pem` file. Move it somewhere safe:

```bash
mv ~/Downloads/*.private-key.pem ~/.cfleet/github-app.pem
chmod 600 ~/.cfleet/github-app.pem
```

This key is the secret. It stays on the fleet server. Don't commit it, don't
copy it to workers.

---

## 4. Install the app on your repos

Go to: **https://github.com/settings/apps/<APP_NAME>/installations**

Click **Install**, then choose:

- **All repositories** — if you want workers to access any repo in the account
- **Only select repositories** — pick the specific repos workers will use

After installing, you'll be redirected to a URL like:
```
https://github.com/settings/installations/12345678
```

That number (`12345678`) is the **Installation ID**. Copy it.

You can also find it later at:
`https://github.com/settings/installations` → click Configure on the app → URL contains the ID.

---

## 5. Configure fleet

### Option A: Interactive

```bash
cfleet gh setup
```

It will prompt for:
- **GitHub App ID** — the number from step 2
- **Installation ID** — the number from step 4
- **Private key path** — enter the path or press Enter if you already placed it at `~/.cfleet/github-app.pem`

### Option B: Manual

Edit `~/.cfleet/config.yml`:

```yaml
github:
  app_id: "123456"
  installation_id: "12345678"
  private_key_path: ~/.cfleet/github-app.pem
```

---

## 6. Set worker access levels

Workers default to `none` (no GitHub access). Set a level per worker:

```bash
# Read-only: can clone, read files, read metadata
cfleet gh set my-worker read

# Triage: read + can create/comment on issues
cfleet gh set my-worker triage

# Write: read + write contents, create PRs, manage issues
cfleet gh set my-worker write
```

Check a worker's level:

```bash
cfleet gh get my-worker
```

Or set it at spawn time:

```bash
cfleet spawn my-worker --gh write
```

### Permission mapping

| Level   | contents | metadata | issues | pull_requests |
|---------|----------|----------|--------|---------------|
| none    | —        | —        | —      | —             |
| read    | read     | read     | —      | —             |
| triage  | read     | read     | write  | —             |
| write   | write    | read     | write  | write         |

---

## 7. How it works at runtime

1. Worker relay requests a token: `POST /api/github/token` with `worker_name`
2. Server checks the worker's `github_level` in state
3. Server generates a JWT signed with the app private key
4. Server exchanges the JWT for a scoped installation token via GitHub API
5. Token returned to worker (expires in 1 hour, auto-renewed)
6. Every token request is logged in state (`cfleet gh log`)

The private key never leaves the server. Workers only get short-lived tokens
scoped to their level.

---

## 8. Verify it works

```bash
# Start the server if not running
cfleet serve &

# Spawn a worker and give it read access
cfleet spawn test-gh
cfleet gh set test-gh read

# Request a token via the API
TOKEN=$(grep 'token:' ~/.cfleet/config.yml | tail -1 | awk '{print $2}')
curl -s -X POST http://127.0.0.1:8420/api/github/token \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"worker_name": "test-gh"}' | jq .
```

Should return:
```json
{
  "token": "ghs_xxxxxxxxxxxx",
  "expires_at": "2026-05-25T09:30:00Z",
  "permissions": {"contents": "read", "metadata": "read"},
  "level": "read"
}
```

Check the audit log:
```bash
cfleet gh log
```

---

## 9. Branch protection warning

When you set a worker to `write` level, fleet checks if the target repos have
branch protection enabled on their default branch. If they don't, you'll see a
warning. **Set up branch protection rules** before giving workers write access:

- Go to repo → Settings → Branches → Add rule
- Branch name pattern: `main` (or your default branch)
- Enable: Require pull request reviews, Require status checks
- This prevents workers from pushing directly to main

---

## Troubleshooting

**"GitHub App not configured"**
→ Run `cfleet gh setup` or check `~/.cfleet/config.yml` has `app_id` and `installation_id` set.

**"Private key not found"**
→ Place the `.pem` at `~/.cfleet/github-app.pem` (or wherever `private_key_path` points).

**"GitHub API error (401)"**
→ Private key doesn't match the app. Re-download from app settings.

**"GitHub API error (403): Resource not accessible by installation"**
→ The app doesn't have the required permission, or it's not installed on the target repo.
Go to app settings → Permissions and check they match the table in step 1.

**"Worker has github_level=none"**
→ Run `cfleet gh set <worker> read` (or triage/write).
