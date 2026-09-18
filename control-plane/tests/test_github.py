import httpx
import pytest

from app.github import GitHubAPI

INSTALL_ID = 162232685


def _installation():
    return {"id": INSTALL_ID, "app_id": 123456}


def _token_resp():
    return {"token": "inst-token", "expires_at": "2099-01-01T00:00:00Z"}


def make_transport():
    """Correct GitHub handshake: GET /installation then POST /access_tokens."""

    def handler(request):
        path = request.url.path
        if path == "/orgs/octo-org/installation":
            return httpx.Response(200, json=_installation())
        if path == f"/app/installations/{INSTALL_ID}/access_tokens":
            return httpx.Response(201, json=_token_resp())
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
    assert await gh.install_token() == token  # cached for next call


@pytest.mark.asyncio
async def test_install_token_does_post_access_tokens(cfg):
    """Regression: old code read token from GET /installation (no token there)."""
    seen = []

    def handler(request):
        path = request.url.path
        if path == "/orgs/octo-org/installation":
            seen.append(("GET", "installation"))
            return httpx.Response(200, json=_installation())
        if path == f"/app/installations/{INSTALL_ID}/access_tokens":
            seen.append(("POST", "access_tokens"))
            return httpx.Response(201, json=_token_resp())
        raise AssertionError(f"unexpected request {request.method} {path}")

    gh = make_gh(cfg, httpx.MockTransport(handler))
    assert await gh.install_token() == "inst-token"
    assert ("GET", "installation") in seen
    assert ("POST", "access_tokens") in seen


@pytest.mark.asyncio
async def test_install_token_missing_token_reuses_cached(cfg):
    """Regression: a no-token 2xx must reuse the cached token, not raise KeyError."""
    calls = {"n": 0}

    def handler(request):
        path = request.url.path
        if path == "/orgs/octo-org/installation":
            return httpx.Response(200, json=_installation())
        if path == f"/app/installations/{INSTALL_ID}/access_tokens":
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(201, json=_token_resp())
            # throttled: 2xx without token
            return httpx.Response(200, json={"message": "rate limited"})
        raise AssertionError(f"unexpected request {request.method} {path}")

    gh = make_gh(cfg, httpx.MockTransport(handler))
    assert await gh.install_token() == "inst-token"
    assert await gh.install_token() == "inst-token"


@pytest.mark.asyncio
async def test_install_token_missing_token_without_cache_raises(cfg):
    def handler(request):
        path = request.url.path
        if path == "/orgs/octo-org/installation":
            return httpx.Response(200, json=_installation())
        if path == f"/app/installations/{INSTALL_ID}/access_tokens":
            return httpx.Response(200, json={"message": "rate limited"})
        raise AssertionError(f"unexpected request {request.method} {path}")

    gh = make_gh(cfg, httpx.MockTransport(handler))
    with pytest.raises(RuntimeError):
        await gh.install_token()


def test_jwt_iss_is_string(cfg):
    """Regression: gh_app_id is an int; PyJWT requires iss to be a string.

    Exercises the real GitHubAPI._jwt code path: the old code passed an int
    and PyJWT raised TypeError: Issuer (iss) must be a string.
    """

    import jwt as pyjwt
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    gh = GitHubAPI(cfg)
    gh.cfg.gh_private_key_pem = pem
    token = gh._jwt()
    payload = pyjwt.decode(token, options={"verify_signature": False})
    assert isinstance(payload["iss"], str)
    assert payload["iss"] == "123456"


@pytest.mark.asyncio
async def test_ensure_runner_group_creates(cfg):
    gh = make_gh(cfg)
    assert await gh.ensure_runner_group() == 7


@pytest.mark.asyncio
async def test_ensure_runner_group_reuses(cfg):
    def handler(request):
        path = request.url.path
        if path == "/orgs/octo-org/installation":
            return httpx.Response(200, json=_installation())
        if path == f"/app/installations/{INSTALL_ID}/access_tokens":
            return httpx.Response(201, json=_token_resp())
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
            return httpx.Response(200, json=_installation())
        if path == f"/app/installations/{INSTALL_ID}/access_tokens":
            return httpx.Response(201, json=_token_resp())
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
        path = request.url.path
        if path == "/orgs/octo-org/installation":
            return httpx.Response(200, json=_installation())
        if path == f"/app/installations/{INSTALL_ID}/access_tokens":
            return httpx.Response(201, json=_token_resp())
        if path.startswith("/repos/octo-org/demo-repo/actions/jobs/"):
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
            return httpx.Response(200, json=_installation())
        if path == f"/app/installations/{INSTALL_ID}/access_tokens":
            return httpx.Response(201, json=_token_resp())
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
            return httpx.Response(200, json=_installation())
        if path == f"/app/installations/{INSTALL_ID}/access_tokens":
            return httpx.Response(201, json=_token_resp())
        if path == "/orgs/octo-org/actions/runners/9":
            return httpx.Response(204)
        raise AssertionError(f"unexpected request {request.method} {path}")

    gh = make_gh(cfg, httpx.MockTransport(handler))
    assert await gh.delete_runner(9) is None
