import time
from unittest.mock import AsyncMock

import pytest

from app.scheduler import RunnerService


async def make_svc(cfg):
    svc = RunnerService(cfg)
    svc.gh = AsyncMock()
    svc.gh.get_job.return_value = None
    svc.gh.list_runners.return_value = []
    svc.k8s = AsyncMock()
    svc.k8s.get_pod.return_value = None
    svc.k8s.list_labelled_nodes = AsyncMock(return_value=["node-a", "node-b"])
    return svc


def job_dict(cfg, job_id, status="pending", created_at=None):
    return {
        "id": str(job_id),
        "name": "demo",
        "repo": "octo-org/demo-repo",
        "labels": [cfg.runner_label],
        "status": status,
        "created_at": created_at if created_at is not None else time.time(),
        "started_at": None,
        "completed_at": None,
        "conclusion": None,
    }


def worker_dict(cfg, name, status="pending", created_at=None):
    return {
        "name": name,
        "job_id": "0",
        "labels": [cfg.runner_label],
        "status": status,
        "created_at": created_at if created_at is not None else time.time(),
        "completed_at": None,
        "pod_phase": None,
        "registered": False,
        "gh_runner_id": None,
        "failure_info": None,
    }


@pytest.mark.asyncio
async def test_demand_match_provisions_pod(cfg):
    svc = await make_svc(cfg)
    svc.gh.create_jit_config = AsyncMock(return_value={"encoded_jit_config": "enc"})
    svc.k8s.create_pod = AsyncMock(return_value=None)
    svc.state.jobs["77"] = job_dict(cfg, 77)
    await svc.reconcile()
    assert "ruyici-runner-77" in svc.state.workers
    worker = svc.state.workers["ruyici-runner-77"]
    assert worker["job_id"] == "77"
    assert worker["status"] == "pending"
    svc.k8s.create_pod.assert_awaited_once()
    pod = svc.k8s.create_pod.await_args.args[0]
    env = {e["name"]: e["value"] for e in pod["spec"]["containers"][0]["env"]}
    assert env["RUNNER_JITCONFIG"] == "enc"
    assert pod["spec"]["nodeSelector"]["ruyici.dev/board"] == "scaleway-em-rv1"


@pytest.mark.asyncio
async def test_supply_meets_demand_skips(cfg):
    svc = await make_svc(cfg)
    svc.gh.create_jit_config = AsyncMock(return_value={"encoded_jit_config": "enc"})
    svc.k8s.create_pod = AsyncMock(return_value=None)
    svc.state.jobs["2"] = job_dict(cfg, 2)
    svc.state.workers["ruyici-runner-1"] = worker_dict(
        cfg, "ruyici-runner-1", status="running"
    )
    await svc.reconcile()
    svc.k8s.create_pod.assert_not_awaited()
    svc.gh.create_jit_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_max_workers_cap(cfg):
    cfg.max_workers = 0
    svc = await make_svc(cfg)
    svc.gh.create_jit_config = AsyncMock(return_value={"encoded_jit_config": "enc"})
    svc.k8s.create_pod = AsyncMock(return_value=None)
    svc.state.jobs["1"] = job_dict(cfg, 1)
    await svc.reconcile()
    svc.k8s.create_pod.assert_not_awaited()
    svc.gh.create_jit_config.assert_not_awaited()


@pytest.mark.asyncio
async def test_never_registered_fails_and_kills_pod(cfg):
    svc = await make_svc(cfg)
    svc.k8s.get_pod.return_value = {"status": {"phase": "Running"}}
    svc.k8s.delete_pod = AsyncMock(return_value=None)
    old = time.time() - 200
    svc.state.workers["ruyici-runner-77"] = worker_dict(
        cfg, "ruyici-runner-77", status="running", created_at=old
    )
    await svc.reconcile()
    w = svc.state.workers["ruyici-runner-77"]
    assert w["status"] == "failed"
    assert w["failure_info"]
    svc.k8s.delete_pod.assert_awaited_once()


@pytest.mark.asyncio
async def test_idle_runner_cleanup(cfg):
    svc = await make_svc(cfg)
    svc.k8s.get_pod.return_value = {"status": {"phase": "Running"}}
    svc.k8s.delete_pod = AsyncMock(return_value=None)
    svc.gh.list_runners.return_value = [
        {"id": 5, "name": "ruyici-runner-77", "busy": False}
    ]
    svc.gh.delete_runner = AsyncMock(return_value=None)
    old = time.time() - 700
    svc.state.workers["ruyici-runner-77"] = worker_dict(
        cfg, "ruyici-runner-77", status="running", created_at=old
    )
    await svc.reconcile()
    w = svc.state.workers["ruyici-runner-77"]
    assert w["status"] == "failed"
    svc.k8s.delete_pod.assert_awaited_once()
    svc.gh.delete_runner.assert_awaited_once_with(5)


@pytest.mark.asyncio
async def test_busy_runner_not_killed(cfg):
    svc = await make_svc(cfg)
    svc.k8s.get_pod.return_value = {"status": {"phase": "Running"}}
    svc.k8s.delete_pod = AsyncMock(return_value=None)
    svc.gh.list_runners.return_value = [
        {"id": 5, "name": "ruyici-runner-77", "busy": True}
    ]
    old = time.time() - 700
    svc.state.workers["ruyici-runner-77"] = worker_dict(
        cfg, "ruyici-runner-77", status="running", created_at=old
    )
    await svc.reconcile()
    w = svc.state.workers["ruyici-runner-77"]
    assert w["status"] == "running"
    svc.k8s.delete_pod.assert_not_awaited()


@pytest.mark.asyncio
async def test_pod_succeeded_completes_worker(cfg):
    svc = await make_svc(cfg)
    svc.k8s.get_pod.return_value = {"status": {"phase": "Succeeded"}}
    svc.state.workers["ruyici-runner-77"] = worker_dict(
        cfg, "ruyici-runner-77", status="running"
    )
    await svc.reconcile()
    assert svc.state.workers["ruyici-runner-77"]["status"] == "completed"


@pytest.mark.asyncio
async def test_pod_failed_fails_worker(cfg):
    svc = await make_svc(cfg)
    svc.k8s.get_pod.return_value = {"status": {"phase": "Failed"}}
    svc.k8s.delete_pod = AsyncMock(return_value=None)
    svc.state.workers["ruyici-runner-77"] = worker_dict(
        cfg, "ruyici-runner-77", status="running"
    )
    await svc.reconcile()
    assert svc.state.workers["ruyici-runner-77"]["status"] == "failed"
    svc.k8s.delete_pod.assert_awaited_once()


@pytest.mark.asyncio
async def test_stuck_queued_job_failed(cfg):
    svc = await make_svc(cfg)
    svc.gh.get_job.return_value = {"status": "completed", "conclusion": "success"}
    svc.state.jobs["1"] = job_dict(cfg, 1, created_at=time.time() - 700)
    await svc.reconcile()
    assert svc.state.jobs["1"]["status"] == "failed"
    assert svc.state.jobs["1"]["conclusion"] == "stuck_queued"


@pytest.mark.asyncio
async def test_terminal_cleanup(cfg):
    cfg.pod_delete_grace = 0
    svc = await make_svc(cfg)
    svc.gh.delete_runner = AsyncMock(return_value=None)
    svc.k8s.delete_pod = AsyncMock(return_value=None)
    w = worker_dict(cfg, "ruyici-runner-77", status="completed")
    w["gh_runner_id"] = 9
    w["completed_at"] = time.time() - 10
    svc.state.workers["ruyici-runner-77"] = w
    await svc.reconcile()
    assert "ruyici-runner-77" not in svc.state.workers
    svc.gh.delete_runner.assert_awaited_once_with(9)
    svc.k8s.delete_pod.assert_awaited_once_with("ruyici-runner-77")
