import logging
import os

import httpx
import yaml

logger = logging.getLogger("ruyici.k8s")


class KubeConfigError(RuntimeError):
    pass


class KubeConfig:
    def __init__(self, server, token=None, insecure=False):
        self.server = server
        self.token = token
        self.insecure = insecure


def load_kubeconfig(path):
    """Parse a kubeconfig file and return the current-context connection."""
    if not path:
        raise KubeConfigError(
            "no kubeconfig configured (set RUIYICI_KUBECONFIG or KUBECONFIG)"
        )
    try:
        with open(path) as f:
            data = yaml.safe_load(f) or {}
    except OSError as exc:
        raise KubeConfigError(f"cannot read kubeconfig {path}: {exc}") from exc
    ctx_name = data.get("current-context")
    if not ctx_name:
        raise KubeConfigError("kubeconfig has no current-context")
    contexts = {c["name"]: c["context"] for c in data.get("contexts", [])}
    users = {u["name"]: u["user"] for u in data.get("users", [])}
    clusters = {c["name"]: c["cluster"] for c in data.get("clusters", [])}
    ctx = contexts.get(ctx_name)
    if not ctx:
        raise KubeConfigError(f"context {ctx_name} not found")
    cluster = clusters.get(ctx["cluster"])
    if not cluster:
        raise KubeConfigError(f"cluster {ctx['cluster']} not found")
    user = users.get(ctx["user"], {})
    server = cluster.get("server", "").rstrip("/")
    token = user.get("token")
    token_file = user.get("tokenFile")
    if not token and token_file:
        with open(token_file) as f:
            token = f.read().strip()
    insecure = bool(cluster.get("insecure-skip-tls-verify", False))
    return KubeConfig(server, token, insecure)


def infer_kubeconfig_path():
    return os.environ.get("RUIYICI_KUBECONFIG") or os.environ.get("KUBECONFIG") or ""


def runner_pod_spec(cfg, name, jit_config):
    """Pod spec for one ephemeral runner, mirroring the RISE provisioning model."""
    board_key, board_value = cfg.board_label.split("=", 1)
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "labels": {"app": cfg.app_label, board_key: board_value},
        },
        "spec": {
            "nodeSelector": {board_key: board_value},
            "affinity": {
                "podAntiAffinity": {
                    "requiredDuringSchedulingIgnoredDuringExecution": [
                        {
                            "labelSelector": {"matchLabels": {"app": cfg.app_label}},
                            "topologyKey": "kubernetes.io/hostname",
                        }
                    ]
                }
            },
            "hostNetwork": True,
            "restartPolicy": "Never",
            "securityContext": {"privileged": True},
            "containers": [
                {
                    "name": "runner",
                    "image": cfg.image,
                    "env": [
                        {"name": "RUNNER_JITCONFIG", "value": jit_config},
                        {"name": "RUNNER_WAIT_FOR_DOCKER_IN_SECONDS", "value": "60"},
                    ],
                    "securityContext": {"privileged": True},
                }
            ],
        },
    }


class KubernetesAPI:
    """Minimal Kubernetes API client (raw HTTP to the apiserver)."""

    def __init__(self, config):
        self.cfg = config
        self._client = None
        self._kube = None

    def _ensure_client(self):
        if self._client is None:
            if self.cfg.kubeconfig:
                self._kube = load_kubeconfig(self.cfg.kubeconfig)
            else:
                self._kube = self._incluster()
            headers = {}
            if self._kube.token:
                headers["Authorization"] = f"Bearer {self._kube.token}"
            verify = True
            if self._kube.insecure:
                verify = False
            self._client = httpx.AsyncClient(
                base_url=self._kube.server,
                verify=verify,
                timeout=30.0,
                headers=headers or None,
            )
        return self._client

    def _incluster(self):
        host = os.environ.get("KUBERNETES_SERVICE_HOST")
        port = os.environ.get("KUBERNETES_SERVICE_PORT", "443")
        token_path = "/var/run/secrets/kubernetes.io/serviceaccount/token"
        if not host or not os.path.exists(token_path):
            raise KubeConfigError("no kubeconfig or in-cluster environment available")
        with open(token_path) as f:
            token = f.read().strip()
        return KubeConfig(f"https://{host}:{port}", token, insecure=True)

    async def _request(self, method, path, **kwargs):
        client = self._ensure_client()
        resp = await client.request(method, path, **kwargs)
        if resp.status_code == 404 and method in ("GET", "DELETE"):
            return None
        resp.raise_for_status()
        if resp.status_code == 204:
            return None
        return resp.json()

    async def create_pod(self, pod):
        return await self._request(
            "POST", f"/api/v1/namespaces/{self.cfg.k8s_namespace}/pods", json=pod
        )

    async def get_pod(self, name):
        return await self._request(
            "GET", f"/api/v1/namespaces/{self.cfg.k8s_namespace}/pods/{name}"
        )

    async def delete_pod(self, name):
        return await self._request(
            "DELETE", f"/api/v1/namespaces/{self.cfg.k8s_namespace}/pods/{name}"
        )
