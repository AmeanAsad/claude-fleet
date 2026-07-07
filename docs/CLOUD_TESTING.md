# Cloud Testing Prep

What you need set up before testing GCP and Azure paths.

---

## GCP

### Prerequisites

1. **GCP project** with Compute Engine API enabled:
   ```bash
   gcloud services enable compute.googleapis.com
   ```

2. **Auth**:
   ```bash
   gcloud auth login
   gcloud auth application-default login
   gcloud config set project <YOUR_PROJECT>
   ```

3. **Firewall rule** — allow SSH from your IP (default VPC usually has this):
   ```bash
   gcloud compute firewall-rules describe default-allow-ssh
   # If missing:
   gcloud compute firewall-rules create allow-ssh --allow tcp:22 --source-ranges 0.0.0.0/0
   ```

4. **SSH key** — fleet uses `~/.ssh/id_ed25519` by default:
   ```bash
   ls ~/.ssh/id_ed25519 || ssh-keygen -t ed25519 -N "" -f ~/.ssh/id_ed25519
   ```

5. **Quota** — default GCP projects have plenty of CPU quota. If testing
   multiple VMs, check: Compute Engine → Quotas → "CPUs (all regions)".

### Config

```yaml
# ~/.cfleet/config.yml
anthropic_api_key: "sk-ant-..."
cloud:
  provider: gcp
  region: us-central1
  ssh_user: ubuntu
  instance_type: e2-standard-4   # 4 vCPU, 16GB — good for 2-3 workers
  ssh_key: ~/.ssh/id_ed25519
  gcp:
    project_id: "your-project-id"
    zone: us-central1-a
```

### Quick test

```bash
cfleet machine create gcp-test --provider gcp
cfleet spawn test-agent --machine gcp-test
cfleet ask test-agent "What OS are you running? One sentence."
cfleet logs test-agent
cfleet kill test-agent --remove-machine
```

### Cost

- `e2-standard-4`: ~$0.13/hr
- `e2-standard-2`: ~$0.07/hr (enough for 1 worker)
- Destroy machines when done — there's no auto-shutdown

---

## Azure

### Prerequisites

1. **Azure subscription** with Compute resource provider registered:
   ```bash
   az provider register --namespace Microsoft.Compute
   az provider register --namespace Microsoft.Network
   ```

2. **Auth**:
   ```bash
   az login
   az account set --subscription <YOUR_SUBSCRIPTION_ID>
   # Get your subscription ID:
   az account show --query id -o tsv
   ```

3. **SSH key** — same as GCP, `~/.ssh/id_ed25519`.

4. **Quota** — Azure has per-region vCPU quotas. For Standard_D4s_v3 (4 vCPU),
   check: Portal → Subscriptions → Usage + quotas → filter "Standard Dv3".
   
   For SNP VMs (Standard_DC4as_v5), check "DCasv5" family quota.

### Config

```yaml
# ~/.cfleet/config.yml
anthropic_api_key: "sk-ant-..."
cloud:
  provider: azure
  region: eastus
  ssh_user: azureuser
  instance_type: Standard_D4s_v3   # 4 vCPU, 16GB
  ssh_key: ~/.ssh/id_ed25519
  azure:
    subscription_id: "xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx"
    resource_group: ""   # auto-generated on init, or set manually
```

The resource group is created automatically during `cfleet init` (with a random
slug prefix). You can also set it manually — fleet creates it if it doesn't exist.

### Quick test

```bash
cfleet machine create az-test --provider azure
cfleet spawn test-agent --machine az-test
cfleet ask test-agent "What OS are you running? One sentence."
cfleet logs test-agent
cfleet kill test-agent --remove-machine
```

### SNP (AMD SEV-SNP confidential VM)

```bash
cfleet machine create az-snp --provider azure --type snp --region eastus
cfleet spawn snp-agent --machine az-snp
cfleet ask snp-agent "Run: dmesg | grep -i sev"
# Should see SEV-SNP attestation messages
cfleet kill snp-agent --remove-machine
```

### Cost

- `Standard_D4s_v3`: ~$0.19/hr
- `Standard_D2s_v3`: ~$0.10/hr (enough for 1 worker)
- `Standard_DC4as_v5` (SNP): ~$0.36/hr
- Destroy machines when done

---

## Multi-worker test (either provider)

This is the key new feature — multiple workers on one VM.

```bash
# Create one machine
cfleet machine create box1 --provider gcp

# Spawn 3 workers on it
cfleet spawn agent-1 --machine box1
cfleet spawn agent-2 --machine box1 --model claude-sonnet-4-6
cfleet spawn agent-3 --machine box1

# Verify isolation
cfleet machine ls     # box1 with 3 workers
cfleet ls             # 3 workers, all on box1

# Send prompts to all three
cfleet ask agent-1 "Create /workspace/agent-1/hello.txt with 'I am agent 1'"
cfleet ask agent-2 "What model are you?"
cfleet ask agent-3 "List files in /workspace/"

# Check agent-3 can't see agent-1's workspace
cfleet ask agent-3 "Does /workspace/agent-1/ exist? Can you read its files?"

# Kill one, others stay alive
cfleet kill agent-2
cfleet ls             # agent-1 and agent-3 still running

# Clean up everything
cfleet kill agent-1
cfleet kill agent-3 --remove-machine
```

---

## Checklist before testing

- [ ] `~/.cfleet/config.yml` has a real `anthropic_api_key`
- [ ] SSH key exists at the configured path
- [ ] Cloud auth is set up (`gcloud auth` / `az login`)
- [ ] Server is running: `cfleet serve &`
- [ ] Server token is in config (auto-generated on first `cfleet serve`)
- [ ] (Optional) GitHub App configured if testing `--gh` flag

## After testing

```bash
# Make sure no VMs are left running
cfleet machine ls              # should be empty

# Double-check cloud console
gcloud compute instances list --filter="name~cfleet"
az vm list --query "[?contains(name, 'cfleet')]" -o table

# Stop the server
pkill -f "cfleet serve"
```
