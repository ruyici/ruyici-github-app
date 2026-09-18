import pytest

from app.config import Config


@pytest.fixture
def cfg():
    c = Config()
    c.gh_app_id = 123456
    c.gh_webhook_secret = "test-secret"
    c.gh_org = "octo-org"
    c.gh_private_key_pem = (
        "-----BEGIN RSA PRIVATE KEY-----\nfake\n-----END RSA PRIVATE KEY-----"
    )
    c.gh_api = "https://api.github.com"
    c.runner_label = "ubuntu-24.04-riscv"
    c.runner_group = "ruyici-runners"
    c.runner_prefix = "ruyici-runner-"
    c.max_workers = 2
    c.poll_interval = 0.01
    c.registration_timeout = 120
    c.pod_pending_timeout = 600
    c.runner_idle_timeout = 600
    c.stuck_queued_min_age = 600
    c.pod_delete_grace = 21600
    c.kubeconfig = ""
    c.k8s_namespace = "default"
    c.board_label = "ruyici.dev/board=scaleway-em-rv1"
    c.app_label = "ruyici-runner"
    c.image = "ruyici/runner:latest"
    return c
