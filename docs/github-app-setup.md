# GitHub App setup checklist — ruyici

Go through this in order. Every checkbox has a concrete verification so you know
the App is correct before the control plane goes live.

## 1. Create the App

- [ ] Owner: your GitHub organization (not a personal account)
- [ ] Settings → Developer settings → GitHub Apps → **New GitHub App**
- [ ] App name: e.g. `ruyici-riscv-runners`
- [ ] Homepage URL: any reachable URL (e.g. your org's site)
- [ ] **Webhook URL**: `https://<control-plane-host>/` (the `POST /` route)
- [ ] **Webhook secret**: a long random value; reuse it as `RUIYICI_GH_WEBHOOK_SECRET`
- [ ] Repository permissions:
  - [ ] **Metadata**: Read
  - [ ] **Actions**: Read
  - [ ] **Organization self-hosted runners**: Read & write
- [ ] Subscribe to events: **Workflow jobs** checked
- [ ] "Where can this App be installed?" → **Any account**
- [ ] Create App

Verify: the App page shows webhook active, secret set, and the three permissions.

## 2. Private key

- [ ] **Generate a private key** (PEM download)
- [ ] Store the PEM in your secret manager; file path goes to
      `RUIYICI_GH_APP_PRIVATE_KEY_FILE`
- [ ] Rotation plan if it leaks (App → Edit → Generate new private key)

Verify: `openssl pkey -in <pem> -noout` succeeds (valid RSA key).

## 3. Install on the organization

- [ ] App page → **Install** → choose the org
- [ ] Repository access: All repos (or selected repos that will run workflows)

Verify: org Settings → Apps → Configure shows the install.

## 4. Permissions mapping to code

| Code needs | GitHub App permission | Env var |
| --- | --- | --- |
| Register/delete self-hosted runners | Organization self-hosted runners R/W | `RUIYICI_GH_ORG` |
| Read job state (`workflow_job` payloads) | Workflow jobs event + Actions R | webhook |
| Resolve org installation & token | Metadata R | — |
| Create/use runner group | Organization self-hosted runners R/W | `RUIYICI_RUNNER_GROUP` |

## 5. Post-install checklist (before live workflow)

- [ ] GitHub webhook delivery shows **200** in the App's "Recent Deliveries" for at
      least the `ping` event
- [ ] A synthetic `POST /` event returns `{"ok":true}` (see DEPLOY.md step 5)
- [ ] First live job: the control-plane log prints
      `created runner group ruyici-runners (id=...)`

## Phase 2 (not MVP)

- [ ] Personal-account variant: separate App requiring **Repository
      Administration Read & write**; `generate-jitconfig` becomes repo-scoped.