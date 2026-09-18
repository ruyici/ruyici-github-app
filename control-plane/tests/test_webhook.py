import json

import httpx
import pytest

from app.main import create_app
from app.scheduler import RunnerService
from app.webhook import sign_payload, verify_signature


def sign(secret, body):
    return f"sha256={sign_payload(secret, body)}"


def event_headers(secret, body, gh_event="workflow_job"):
    return {
        "X-GitHub-Event": gh_event,
        "X-Hub-Signature-256": sign(secret, body),
        "Content-Type": "application/json",
    }


def queued_payload(job_id, labels):
    return {
        "action": "queued",
        "workflow_job": {
            "id": job_id,
            "name": "demo",
            "labels": labels,
            "status": "queued",
        },
        "repository": {"full_name": "octo-org/demo-repo"},
    }


@pytest.mark.asyncio
async def test_verify_signature(cfg):
    body = b'{"a": 1}'
    good = sign(cfg.gh_webhook_secret, body)
    assert verify_signature(cfg.gh_webhook_secret, body, good)
    assert not verify_signature(cfg.gh_webhook_secret, b"tampered", good)
    assert not verify_signature(cfg.gh_webhook_secret, body, "sha256=deadbeef")
    assert not verify_signature(cfg.gh_webhook_secret, body, "")
    assert not verify_signature(cfg.gh_webhook_secret, body, "md5=abc")


@pytest.mark.asyncio
async def test_bad_signature_rejected(cfg):
    svc = RunnerService(cfg)
    app = create_app(cfg=cfg, service=svc)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        body = b'{"action": "queued"}'
        resp = await client.post(
            "/",
            content=body,
            headers={
                "X-GitHub-Event": "workflow_job",
                "X-Hub-Signature-256": "sha256=wrong",
            },
        )
        assert resp.status_code == 401
        assert svc.state.jobs == {}


@pytest.mark.asyncio
async def test_queued_records_job(cfg):
    svc = RunnerService(cfg)
    app = create_app(cfg=cfg, service=svc)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        payload = queued_payload(77, [cfg.runner_label])
        body = json.dumps(payload).encode()
        resp = await client.post(
            "/", content=body, headers=event_headers(cfg.gh_webhook_secret, body)
        )
        assert resp.status_code == 200
    job = svc.state.jobs["77"]
    assert job["status"] == "pending"
    assert job["repo"] == "octo-org/demo-repo"
    assert job["labels"] == [cfg.runner_label]


@pytest.mark.asyncio
async def test_multilabel_ignored(cfg):
    svc = RunnerService(cfg)
    app = create_app(cfg=cfg, service=svc)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        payload = queued_payload(88, ["self-hosted", cfg.runner_label])
        body = json.dumps(payload).encode()
        resp = await client.post(
            "/", content=body, headers=event_headers(cfg.gh_webhook_secret, body)
        )
        assert resp.status_code == 200
        assert svc.state.jobs == {}


@pytest.mark.asyncio
async def test_unknown_label_ignored(cfg):
    svc = RunnerService(cfg)
    app = create_app(cfg=cfg, service=svc)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        payload = queued_payload(99, ["other-demo"])
        body = json.dumps(payload).encode()
        resp = await client.post(
            "/", content=body, headers=event_headers(cfg.gh_webhook_secret, body)
        )
        assert resp.status_code == 200
        assert svc.state.jobs == {}


@pytest.mark.asyncio
async def test_state_machine_forward_only(cfg):
    svc = RunnerService(cfg)
    app = create_app(cfg=cfg, service=svc)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        queued = json.dumps(queued_payload(9, [cfg.runner_label])).encode()
        assert (
            await client.post(
                "/",
                content=queued,
                headers=event_headers(cfg.gh_webhook_secret, queued),
            )
        ).status_code == 200
        in_progress = {
            "action": "in_progress",
            "workflow_job": {
                "id": 9,
                "name": "demo",
                "labels": [cfg.runner_label],
                "status": "in_progress",
            },
            "repository": {"full_name": "octo-org/demo-repo"},
        }
        body = json.dumps(in_progress).encode()
        assert (
            await client.post(
                "/", content=body, headers=event_headers(cfg.gh_webhook_secret, body)
            )
        ).status_code == 200
        completed = {
            "action": "completed",
            "workflow_job": {
                "id": 9,
                "name": "demo",
                "labels": [cfg.runner_label],
                "status": "completed",
                "conclusion": "success",
            },
            "repository": {"full_name": "octo-org/demo-repo"},
        }
        body = json.dumps(completed).encode()
        assert (
            await client.post(
                "/", content=body, headers=event_headers(cfg.gh_webhook_secret, body)
            )
        ).status_code == 200
    job = svc.state.jobs["9"]
    assert job["status"] == "completed"
    assert job["conclusion"] == "success"


@pytest.mark.asyncio
async def test_ping_ignored(cfg):
    svc = RunnerService(cfg)
    app = create_app(cfg=cfg, service=svc)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        body = b'{"zen": "hi"}'
        resp = await client.post(
            "/",
            content=body,
            headers=event_headers(cfg.gh_webhook_secret, body, "ping"),
        )
        assert resp.status_code == 200
        assert svc.state.jobs == {}
        assert svc.state.workers == {}


@pytest.mark.asyncio
async def test_redelivery_is_idempotent(cfg):
    svc = RunnerService(cfg)
    app = create_app(cfg=cfg, service=svc)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        body = json.dumps(queued_payload(5, [cfg.runner_label])).encode()
        headers = event_headers(cfg.gh_webhook_secret, body)
        await client.post("/", content=body, headers=headers)
        await client.post("/", content=body, headers=headers)
        assert len(svc.state.jobs) == 1
