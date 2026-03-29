# Scoped Cloud Roles for Testing

How to create isolated, scoped credentials for Azure and GCP so that test automation (or agents like Claude Code) can only touch a sandbox — not your production resources.

## Azure: Scoped Service Principal

Azure resource groups are the isolation boundary. A service principal scoped to one RG cannot see or modify anything outside it.

```bash
# 1. Get your subscription ID
az account show --query id -o tsv

# 2. Create a dedicated resource group
az group create -n claude-sandbox -l westeurope

# 3. Create a service principal scoped to ONLY that RG
az ad sp create-for-rbac --name claude-sandbox-sp \
  --role Contributor \
  --scopes /subscriptions/YOUR_SUB_ID/resourceGroups/claude-sandbox
```

Output:
```json
{
  "appId": "xxx",        // client ID
  "password": "xxx",     // client secret
  "tenant": "xxx"        // tenant ID
}
```

### Using the credentials

```bash
az login --service-principal \
  -u APP_ID -p PASSWORD --tenant TENANT_ID
```

### Verifying scope is locked down

```bash
az group list -o table
# Should show ONLY claude-sandbox
```

### Cleanup

```bash
# Delete everything in one shot
az group delete -n claude-sandbox --yes --no-wait

# Delete the service principal
az ad sp delete --id APP_ID
```

### Notes
- RGs are flat (no nesting), but they can hold resources in any region
- The SP gets Contributor on just that RG — full create/delete within it, zero visibility elsewhere
- Share the appId/password/tenant with your team for reuse

---

## GCP: Scoped Service Account in a Dedicated Project

GCP projects are the isolation boundary. A service account in a sandbox project cannot touch other projects.

```bash
# 1. Create a dedicated project (IDs are globally unique)
gcloud projects create claude-sandbox-YOURNAME --name="claude sandbox"

# 2. Link billing
gcloud billing accounts list  # find your billing account ID
gcloud billing projects link claude-sandbox-YOURNAME \
  --billing-account=YOUR_BILLING_ID

# 3. Enable compute API
gcloud services enable compute.googleapis.com \
  --project=claude-sandbox-YOURNAME

# 4. Create a service account
gcloud iam service-accounts create claude-sandbox-sa \
  --project=claude-sandbox-YOURNAME \
  --display-name="claude sandbox"

# 5. Grant compute admin only (minimum for creating VMs)
gcloud projects add-iam-policy-binding claude-sandbox-YOURNAME \
  --member="serviceAccount:claude-sandbox-sa@claude-sandbox-YOURNAME.iam.gserviceaccount.com" \
  --role="roles/compute.admin"

# 6. Create a key file
gcloud iam service-accounts keys create ~/claude-sandbox-sa.json \
  --iam-account=claude-sandbox-sa@claude-sandbox-YOURNAME.iam.gserviceaccount.com \
  --project=claude-sandbox-YOURNAME
```

### If key creation is blocked by org policy

Your org may enforce `iam.disableServiceAccountKeyCreation`. Override it for just this project:

```bash
gcloud resource-manager org-policies disable-enforce \
  iam.disableServiceAccountKeyCreation \
  --project=claude-sandbox-YOURNAME
```

Then retry step 6.

### Using the credentials

```bash
gcloud auth activate-service-account \
  --key-file=~/claude-sandbox-sa.json \
  --project=claude-sandbox-YOURNAME
```

### Cleanup

```bash
# Delete the entire project (removes everything)
gcloud projects delete claude-sandbox-YOURNAME
```

### Notes
- Project IDs are globally unique — append your name or team
- Separate project = zero blast radius to other projects
- The SA only has `roles/compute.admin` — can't touch IAM, storage, etc.
- Delete the whole project when done testing

---

## For cfleet specifically

Put the credentials in `~/.cfleet/config.yml`:

```yaml
cloud:
  provider: azure  # or gcp
  azure:
    subscription_id: "YOUR_SUB_ID"
    resource_group: "claude-sandbox"  # SP is scoped here
  gcp:
    project_id: "claude-sandbox-YOURNAME"
```

For Azure, authenticate via env vars or `az login --service-principal`.
For GCP, set `GOOGLE_APPLICATION_CREDENTIALS=~/.cfleet/gcp-sa-key.json`.
