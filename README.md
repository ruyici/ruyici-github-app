# ruyici: a RISE-style GitHub App runner service

`ruyici` runs GitHub Actions jobs on physical RISC-V nodes using ephemeral
self-hosted runners, modeled after [RISE RISC-V Runners](https://riscv-runners.riseproject.dev/).

Deploy now: [DEPLOY.md](DEPLOY.md) (ordered steps + verification). GitHub App
permission/event toggles: [docs/github-app-setup.md](docs/github-app-setup.md).

Flow:

```
workflow_job webhook → control plane (FastAPI) → k8s pod on a RISC-V node
   → pod registers as a just-in-time runner → job executes → pod deleted
```

## Components

| Path | What it is |
| --- | --- |
| `control-plane/` | FastAPI webhook handler + scheduler (outside the cluster) |
| `runner/` | RISC-V runner image (GitHub-runner RISC-V port + in-pod Docker) |
| `k8s/node-labels.yaml` | Node labeling instructions |
| `demo-workflow.yml` | Sample workflow for consumers (`runs-on: ubuntu-24.04-riscv`) |
| `.github/workflows/ci.yml` | This repo's own CI: lint + unit tests on ubuntu-latest |
| `.github/workflows/runner-demo.yml` | Dogfood: run the same tests on the native RISE runner |
| `scripts/simulate-webhook.py` | Offline webhook simulation for local verification |

## Control plane

Single FastAPI process. In-memory state for the MVP (no Postgres); a 5s
reconcile loop matches pending jobs to node capacity:

1. `_sync_jobs` — fail jobs that are stuck queued past `RUIYICI_STUCK_QUEUED_MIN_AGE`.
2. `_sync_workers` — pod-phase sync, orphan sweep, and health checks
   (never registered, stuck pending, idle runner).
3. `_cleanup_terminal` — drop finished/failed pods and their GitHub runners.
4. `_demand_match` — FIFO; provision a pod when `supply < demand` and under
   `RUIYICI_MAX_WORKERS`.

Exclusive scheduling: each runner pod carries pod anti-affinity on
`topologyKey: kubernetes.io/hostname`, so at most one runner lands on a node
(no device plugin needed for the MVP).

### Environment

| Variable | Default | Purpose |
| --- | --- | --- |
| `RUIYICI_GH_APP_ID` | `0` | GitHub App ID |
| `RUIYICI_GH_WEBHOOK_SECRET` | — | Shared HMAC secret (webhook) |
| `RUIYICI_GH_APP_PRIVATE_KEY` | — | App private key PEM (or `RUIYICI_GH_APP_PRIVATE_KEY_FILE`) |
| `RUIYICI_GH_ORG` | — | Organization that installs the App |
| `RUIYICI_KUBECONFIG` | `KUBECONFIG` | kubeconfig path (control plane runs outside the cluster) |
| `RUIYICI_RUNNER_LABEL` | `ubuntu-24.04-riscv` | The single label the scheduler matches |
| `RUIYICI_BOARD_LABEL` | `ruyici.dev/board=scaleway-em-rv1` | nodeSelector key=value |
| `RUIYICI_RUNNER_IMAGE` | `ruyici/runner:ubuntu-24.04-latest` | Runner image |
| `RUIYICI_MAX_WORKERS` | `20` | Per-org worker cap |
| `RUIYICI_POLL_INTERVAL` | `5` | Reconcile interval (s) |
| `RUIYICI_REGISTRATION_TIMEOUT` | `120` | Kill pod if runner never registers |
| `RUIYICI_POD_PENDING_TIMEOUT` | `600` | Kill pod stuck `Pending` |
| `RUIYICI_RUNNER_IDLE_TIMEOUT` | `600` | Kill runner that never picks up a job |
| `RUIYICI_STUCK_QUEUED_MIN_AGE` | `600` | Mark job failed if still queued |

### Run it

```bash
cd control-plane
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8080
# tests
pip install -r requirements-dev.txt
pytest
```

Offline check without a GitHub App:

```bash
RUIYICI_GH_WEBHOOK_SECRET=anything RUIYICI_KUBECONFIG=/dev/stdin \
  uvicorn app.main:app --port 8080
python3 scripts/simulate-webhook.py --secret anything --action queued   # job recorded
python3 scripts/simulate-webhook.py --secret anything --action completed
curl localhost:8080/jobs
```

Endpoints: `POST /` (webhook), `GET /health`, `GET /jobs`, `GET /workers`,
`GET /usage`.

## GitHub App setup

1. Create a new GitHub App (Settings → Developer settings → GitHub Apps),
   owned by your organization.
2. Webhook URL: `https://<control-plane>/`; Webhook secret: set one.
3. Permissions:
   - Organization self-hosted runners: **Read & write**
   - Metadata: **Read**
   - Actions: **Read**
4. Subscribe to the **Workflow jobs** webhook event.
5. Generate the private key, save the PEM, and rotate it if exposed.
6. Install the App on your organization (All repositories or selected repos).
   The first `generate-jitconfig` call auto-creates the runner group
   `ruyici-runners`; adjust `RUIYICI_RUNNER_GROUP` if you prefer your own.

Personal-account installs need repository Administration read/write instead
of the org-level self-hosted-runners permission; that variant is phase 2.

## Runners

Label the RISC-V nodes (k3s works):

```bash
kubectl label node <node-name> ruyici.dev/board=scaleway-em-rv1
```

Build the runner image on any native riscv64 host:

```bash
cd runner
docker build -t registry.example/repo/ruyici-runner:ubuntu-24.04 -f Dockerfile.riscv64 .
# set RUNNER_DIST_URL to the current release tarball of
# https://github.com/Cloud-V-10xE/github-runner-riscv first
```

Set `RUIYICI_RUNNER_IMAGE` to the pushed image. Pods run privileged with host
network so the embedded `dockerd` can do container building.

## Demo

Add `demo-workflow.yml` to a consumer repo. Push → webhook → pod → job runs
on real `riscv64` → pod cleaned up. `uname -m` prints `riscv64`.

## Known limits (MVP)

- In-memory state: single replica only; crashes lose in-flight view.
- One org-level App; personal-account installs unsupported.
- Anti-affinity instead of a device plugin (fine until you need
  resource-aware allocation).
- Runner image tarball URL is a placeholder (see `runner/Dockerfile.riscv64`).
