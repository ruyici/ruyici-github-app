import httpx
import pytest
from app.github import GitHubAPI


def make_transport():
    def handler(request):
        path = request.url.path
        if path == "/orgs/octo-org/installation":
            body = {"token": "inst-token", "expires_at": "2099-01-01T00:00:00Z"}
            return httpx.Response(200, json=body)
        if path == "/orgs/octo-org/actions/runner-groups":
            if request.method == "GET":
                return httpx.Response(200, json={"total_count": 0, "runner_groups": []})
            return httpx.Response(201, json={"id": 7, "name": "ruyici-runners"})
        if path == "/orgs/octo-org/actions/runners/generate-jitconfig":
            return httpx.Response(
                201,
                json={
                    "runner": {"id": 1, "name": "x"},
                    "encoded_jit_config": "enc-token",
                },
            )
        raise AssertionError(f"unexpected request {request.method} {path}")

    return httpx.MockTransport(handler)


def make_gh(cfg, transport=None):
    gh = GitHubAPI(cfg)
    gh._client = httpx.AsyncClient(
        base_url=cfg.gh_api, transport=transport or make_transport()
    )
    gh._jwt = lambda: "fake.jwt"  # avoid real RSA encoding in tests
    return gh


@pytest.mark.asyncio
async def test_install_token(cfg):
    gh = make_gh(cfg)
    token = await gh.install_token()
    assert token == "inst-token"
    assert await gh.install_token() == token


@pytest.mark.asyncio
async def test_ensure_runner_group_creates(cfg):
    gh = make_gh(cfg)
    assert await gh.ensure_runner_group() == 7


@pytest.mark.asyncio
async def test_ensure_runner_group_reuses(cfg):
    def handler(request):
        path = request.url.path
        if path == "/orgs/octo-org/installation":
            return httpx.Response(
                200, json={"token": "inst-token", "expires_at": "2099-01-01T00:00:00Z"}
            )
        if path == "/orgs/octo-org/actions/runner-groups":
            return httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "runner_groups": [{"id": 3, "name": "ruyici-runners"}],
                },
            )
        raise AssertionError(f"unexpected request {request.method} {path}")

    gh = make_gh(cfg, httpx.MockTransport(handler))
    assert await gh.ensure_runner_group() == 3


@pytest.mark.asyncio
async def test_create_jit_config(cfg):
    gh = make_gh(cfg)
    out = await gh.create_jit_config("ruyici-runner-77", [cfg.runner_label])
    assert out["encoded_jit_config"] == "enc-token"


@pytest.mark.asyncio
async def test_jit_config_error_propagates(cfg):
    def handler(request):
        path = request.url.path
        if path == "/orgs/octo-org/installation":
            return httpx.Response(
                200, json={"token": "inst-token", "expires_at": "2099-01-01T00:00:00Z"}
            )
        if path == "/orgs/octo-org/actions/runner-groups":
            if request.method == "POST":
                return httpx.Response(201, json={"id": 12, "name": "ruyici-runners"})
            return httpx.Response(200, json={"total_count": 0, "runner_groups": []})
        if path == "/orgs/octo-org/actions/runners/generate-jitconfig":
            return httpx.Response(422, json={"message": "Validation failed"})
        raise AssertionError(f"unexpected request {request.method} {path}")

    gh = make_gh(cfg, httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        await gh.create_jit_config("ruyici-runner-78", [cfg.runner_label])


@pytest.mark.asyncio
async def test_get_job_404_returns_none(cfg):
    def handler(request):
        if request.url.path.startswith("/orgs/octo-org/installation"):
            return httpx.Response(
                200, json={"token": "inst-token", "expires_at": "2099-01-01T00:00:00Z"}
            )
        if request.url.path.startswith("/repos/octo-org/demo-repo/actions/jobs/"):
            return httpx.Response(404, json={"message": "Not Found"})
        raise AssertionError(f"unexpected request {request.method} {request.url.path}")

    gh = make_gh(cfg, httpx.MockTransport(handler))
    assert await gh.get_job("octo-org/demo-repo", "404") is None
    assert await gh.get_job("octo-org/demo-repo", "99") is None


@pytest.mark.asyncio
async def test_list_runners_pages(cfg):
    def handler(request):
        path = request.url.path
        if path == "/orgs/octo-org/installation":
            return httpx.Response(
                200, json={"token": "inst-token", "expires_at": "2099-01-01T00:00:00Z"}
            )
        if path == "/orgs/octo-org/actions/runners":
            return httpx.Response(
                200,
                json={
                    "total_count": 1,
                    "runners": [{"id": 9, "name": "ruyici-runner-77", "busy": False}],
                },
            )
        raise AssertionError(f"unexpected request {request.method} {path}")

    gh = make_gh(cfg, httpx.MockTransport(handler))
    runners = await gh.list_runners()
    assert [r["name"] for r in runners] == ["ruyici-runner-77"]


@pytest.mark.asyncio
async def test_delete_runner(cfg):
    def handler(request):
        path = request.url.path
        if path == "/orgs/octo-org/installation":
            return httpx.Response(
                200, json={"token": "inst-token", "expires_at": "2099-01-01T00:00:00Z"}
            )
        if path == "/orgs/octo-org/actions/runners/9":
            return httpx.Response(204)
        raise AssertionError(f"unexpected request {request.method} {path}")

    gh = make_gh(cfg, httpx.MockTransport(handler))
    assert await gh.delete_runner(9) is None
