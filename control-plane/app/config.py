import os
from pathlib import Path


class Config:
    """Runtime configuration, read from the environment once at startup."""

    def __init__(self):
        self.gh_app_id = int(os.environ.get("RUIYICI_GH_APP_ID", "0"))
        self.gh_webhook_secret = os.environ.get("RUIYICI_GH_WEBHOOK_SECRET", "")
        pem = os.environ.get("RUIYICI_GH_APP_PRIVATE_KEY", "")
        key_file = os.environ.get("RUIYICI_GH_APP_PRIVATE_KEY_FILE", "")
        if not pem and key_file:
            pem = Path(key_file).read_text()
        self.gh_private_key_pem = pem
        self.gh_org = os.environ.get("RUIYICI_GH_ORG", "")

        self.runner_label = os.environ.get("RUIYICI_RUNNER_LABEL", "ubuntu-24.04-riscv")
        self.runner_group = os.environ.get("RUIYICI_RUNNER_GROUP", "ruyici-runners")
        self.runner_prefix = os.environ.get("RUIYICI_RUNNER_PREFIX", "ruyici-runner-")
        self.max_workers = int(os.environ.get("RUIYICI_MAX_WORKERS", "20"))

        self.kubeconfig = os.environ.get("RUIYICI_KUBECONFIG", "")
        self.k8s_namespace = os.environ.get("RUIYICI_K8S_NAMESPACE", "default")
        self.board_label = os.environ.get(
            "RUIYICI_BOARD_LABEL", "ruyici.dev/board=scaleway-em-rv1"
        )
        self.app_label = os.environ.get("RUIYICI_APP_LABEL", "ruyici-runner")
        self.image = os.environ.get(
            "RUIYICI_RUNNER_IMAGE", "ruyici/runner:ubuntu-24.04-latest"
        )

        self.gh_api = os.environ.get("RUIYICI_GH_API", "https://api.github.com")
        self.poll_interval = float(os.environ.get("RUIYICI_POLL_INTERVAL", "5"))
        self.registration_timeout = float(
            os.environ.get("RUIYICI_REGISTRATION_TIMEOUT", "120")
        )
        self.pod_pending_timeout = float(
            os.environ.get("RUIYICI_POD_PENDING_TIMEOUT", "600")
        )
        self.runner_idle_timeout = float(
            os.environ.get("RUIYICI_RUNNER_IDLE_TIMEOUT", "600")
        )
        self.stuck_queued_min_age = float(
            os.environ.get("RUIYICI_STUCK_QUEUED_MIN_AGE", "600")
        )
        self.pod_delete_grace = float(
            os.environ.get("RUIYICI_POD_DELETE_GRACE", "21600")
        )
