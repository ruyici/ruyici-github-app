#!/usr/bin/env python3
"""Feed synthetic GitHub workflow_job webhooks to the control plane.

Use this to exercise the webhook path offline before wiring a live GitHub
App. Signatures are created with the shared webhook secret.

Usage:
    MCI_GH_WEBHOOK_SECRET=<secret> python3 scripts/simulate-webhook.py \
        --url http://127.0.0.1:8080/ \
        --action queued --job-id 4242 --label ubuntu-24.04-riscv
"""

import argparse
import hashlib
import hmac
import json
import os
import time
import urllib.request

EVENTS = ("queued", "in_progress", "completed")


def sign(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def build_payload(job_id: int, action: str, label: str) -> dict:
    job = {
        "id": job_id,
        "name": "simulated-job",
        "labels": [label],
        "status": action,
    }
    payload = {
        "action": action,
        "workflow_job": job,
        "repository": {"full_name": "octo-org/demo-repo"},
    }
    if action == "completed":
        job["conclusion"] = "success"
    return payload


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8080/")
    parser.add_argument("--secret", help="webhook secret (or RUIYICI_GH_WEBHOOK_SECRET)", default="")
    parser.add_argument("--job-id", type=int, default=int(time.time() * 1000) % 1_000_000)
    parser.add_argument("--action", choices=EVENTS, default="queued")
    parser.add_argument("--label", default="ubuntu-24.04-riscv")
    args = parser.parse_args()

    secret = args.secret or os.environ.get("RUIYICI_GH_WEBHOOK_SECRET", "")
    body = json.dumps(build_payload(args.job_id, args.action, args.label)).encode()
    req = urllib.request.Request(
        args.url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "workflow_job",
            "X-Hub-Signature-256": f"sha256={sign(secret, body)}",
        },
    )
    with urllib.request.urlopen(req) as resp:
        print(resp.status, resp.read().decode())


if __name__ == "__main__":
    main()