# Deployment for ruyici (RISE-style runner service)

This page is the step-by-step deployment order: **label nodes → GitHub App → runner
image → control plane → offline check → install App → live demo → observe →
dogfood**. Each step tells you how to verify it before moving on.

## 0. Prerequisites

- Kubernetes cluster with RISC-V nodes (k3s is fine). `kubectl` on your control-plane host.
- A GitHub organization that owns the App.
- A public HTTPS endpoint for the control plane (reverse proxy / ingress / tunnel).

Verify:

```bash
kubectl get nodes
```

## 1. Label the RISC-V nodes

The scheduler places pods via `nodeSelector ruyici.dev/board=<pool>`, and pod
anti-affinity keeps one runner per node (no device plugin needed for the MVP).

```bash
kubectl label node <node-name> ruyici.dev/board=scaleway-em-rv1
kubectl get nodes --show-labels | grep ruyici.dev/board
```

Apply on every RISC-V node. The label value must match `RUIYICI_BOARD_LABEL` (default
`scaleway-em-rv1`).

## 2. Create the GitHub App

Follow the exact permission/event toggles in `docs/github-app-setup.md`. Save:

- App ID
- Webhook secret
- Private key PEM (store it in your secret manager, never in the repo)

Configure the App BEFORE building the control plane so the endpoints exist.

## 3. Build & push the runner image

The image must be built **on a real riscv64 host** (see `runner/Dockerfile.riscv64`).
No QEMU in the build path.

```bash
cd runner
# First: edit Dockerfile.riscv64 and set ARG RUNNER_DIST_URL to the current
# Cloud-V-10xE/github-runner-riscv release tarball.
docker build -f Dockerfile.riscv64 -t <registry>/<org>/ruyici-runner:ubuntu-24.04 .
docker push <registry>/<org>/ruyici-runner:ubuntu-24.04
```

Verify the built image is `linux/riscv64`:

```bash
docker inspect <registry>/<org>/ruyici-runner:ubuntu-24.04 --format '{{.Architecture}}'
```

## 4. Deploy the control plane (outside the cluster)

Cluster auth comes from a kubeconfig path (`RUIYICI_KUBECONFIG` or `KUBECONFIG`).

```bash
cd control-plane
pip install -r requirements.txt
```

Set the environment (systemd unit / container / shell):

```bash
export RUIYICI_GH_APP_ID=123456
export RUIYICI_GH_APP_PRIVATE_KEY_FILE=/path/to/private-key.pem
export RUIYICI_GH_WEBHOOK_SECRET=change-me
export RUIYICI_GH_ORG=your-org
export RUIYICI_KUBECONFIG="$HOME/.kube/config"
export RUIYICI_RUNNER_IMAGE=<registry>/<org>/ruyici-runner:ubuntu-24.04
```

Run it:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8080
```

Put HTTPS in front of port 8080 (Caddy/nginx/ingress). The GitHub App webhook URL is
`https://<control-plane-host>/`.

Verify:

```bash
curl https://<control-plane-host>/health     # {"status":"ok"}
```

Other live endpoints: `GET /jobs`, `/workers`, `/usage`.

## 5. Offline check (before the App goes live)

Feed synthetic webhooks and confirm the handler+state machine work. The scheduler
will not act yet because no real GitHub runner group exists — that's fine; this step
only proves the webhook path.

```bash
RUIYICI_GH_WEBHOOK_SECRET=change-me python3 scripts/simulate-webhook.py --action queued  --job-id 4242
RUIYICI_GH_WEBHOOK_SECRET=change-me python3 scripts/simulate-webhook.py --action in_progress --job-id 4242
RUIYICI_GH_WEBHOOK_SECRET=change-me python3 scripts/simulate-webhook.py --action completed --job-id 4242
curl https://<control-plane-host>/jobs        # one job, status=completed
```

## 6. Install the App on the organization

- Settings → Applications → your App → Install on your org.
- Grant access to the repos that will run workflows (or all repos).
- The first `generate-jitconfig` call auto-creates the runner group `ruyici-runners`
  (`RUIYICI_RUNNER_GROUP`, default `ruyici-runners`).

Verify the App can reach the API (watch the control-plane log for the first
`create_jitconfig` call when a job arrives; the runner group line is logged).

## 7. Live smoke test

Add `demo-workflow.yml` to a consumer repo (or this repo):

```yaml
on: workflow_dispatch
jobs:
  demo:
    runs-on: ubuntu-24.04-riscv
    steps:
      - run: uname -m          # must print riscv64
      - run: docker info       # in-pod Docker must be up
```

Trigger it. Success criteria:

- job queued → pod appears → runner registers (watch `kubectl get pods -w`)
- `uname -m` prints `riscv64`
- pod is deleted after the job completes
- `GET /workers` shows the worker lifecycle `pending → running → completed`

## 8. Observe and troubleshoot

- `GET /usage` — demand vs supply vs cap
- `GET /workers` — failure_info for stuck/idle runners
- Check the control-plane log for `JIT config failed`, `max_workers cap`,
  `runner never registered`, `runner idle` lines.

Timeout knobs (env): `RUIYICI_REGISTRATION_TIMEOUT` (kill runner that never
registers), `RUIYICI_RUNNER_IDLE_TIMEOUT` (kill runner that never picks up a job),
`RUIYICI_POD_PENDING_TIMEOUT` (kill stuck pod), `RUIYICI_STUCK_QUEUED_MIN_AGE`.

## 9. Turn on dogfooding

`.github/workflows/runner-demo.yml` runs this repo's pytest suite on the native
RISC-V runners. Push to `main` after step 7 to exercise the full loop for real.

`.github/workflows/ci.yml` (ubuntu-latest lint + unit tests) runs on every PR
automatically.

## 10. Harden (post-MVP)

- Replace in-memory state with PostgreSQL (single-replica limitation).
- Personal-account App variant (repository Administration permission).
- Device plugin for resource-aware exclusive allocation.
- Stage/prod image promotion like RISE (`-staging` / `-prod` tags with approval).
