import httpx
import pytest
import yaml
from app.k8s import KubeConfigError, KubernetesAPI, load_kubeconfig, runner_pod_spec

KUBECONFIG = """
apiVersion: v1
kind: Config
current-context: dev
contexts:
  - name: dev
    context:
      cluster: dev-cluster
      user: dev-user
clusters:
  - name: dev-cluster
    cluster:
      server: https://127.0.0.1:6443
      insecure-skip-tls-verify: true
users:
  - name: dev-user
    user:
      token: test-token
"""


def test_load_kubeconfig(tmp_path):
    path = tmp_path / "config"
    path.write_text(KUBECONFIG)
    kc = load_kubeconfig(str(path))
    assert kc.server == "https://127.0.0.1:6443"
    assert kc.token == "test-token"
    assert kc.insecure


def test_missing_kubeconfig(tmp_path):
    with pytest.raises(KubeConfigError):
        load_kubeconfig(str(tmp_path / "missing"))


def test_empty_kubeconfig(tmp_path):
    path = tmp_path / "config"
    path.write_text(yaml.safe_dump({"apiVersion": "v1", "kind": "Config"}))
    with pytest.raises(KubeConfigError):
        load_kubeconfig(str(path))


def test_runner_pod_spec(cfg):
    pod = runner_pod_spec(cfg, "ruyici-runner-77", "enc")
    assert pod["metadata"]["name"] == "ruyici-runner-77"
    assert pod["spec"]["nodeSelector"] == {"ruyici.dev/board": "scaleway-em-rv1"}
    aa = pod["spec"]["affinity"]["podAntiAffinity"][
        "requiredDuringSchedulingIgnoredDuringExecution"
    ]
    assert aa[0]["topologyKey"] == "kubernetes.io/hostname"
    assert aa[0]["labelSelector"]["matchLabels"]["app"] == "ruyici-runner"
    env = {e["name"]: e["value"] for e in pod["spec"]["containers"][0]["env"]}
    assert env["RUNNER_JITCONFIG"] == "enc"
    assert pod["spec"]["hostNetwork"] is True
    assert pod["spec"]["securityContext"]["privileged"] is True
    assert pod["spec"]["restartPolicy"] == "Never"


def make_k8s(cfg, handler):
    k8s = KubernetesAPI(cfg)
    k8s._client = httpx.AsyncClient(
        base_url="https://127.0.0.1:6443",
        transport=httpx.MockTransport(handler),
        verify=False,
    )
    return k8s


@pytest.mark.asyncio
async def test_create_get_delete_pod(cfg):
    def handler(request):
        path = request.url.path
        if request.method == "POST" and path == "/api/v1/namespaces/default/pods":
            return httpx.Response(201, json={"metadata": {"name": "ruyici-runner-77"}})
        if (
            request.method == "GET"
            and path == "/api/v1/namespaces/default/pods/ruyici-runner-77"
        ):
            return httpx.Response(404, json={"message": "Not Found"})
        if (
            request.method == "DELETE"
            and path == "/api/v1/namespaces/default/pods/ruyici-runner-77"
        ):
            return httpx.Response(204)
        raise AssertionError(f"unexpected request {request.method} {path}")

    k8s = make_k8s(cfg, handler)
    pod = runner_pod_spec(cfg, "ruyici-runner-77", "enc")
    created = await k8s.create_pod(pod)
    assert created["metadata"]["name"] == "ruyici-runner-77"
    assert await k8s.get_pod("ruyici-runner-77") is None
    assert await k8s.delete_pod("ruyici-runner-77") is None
