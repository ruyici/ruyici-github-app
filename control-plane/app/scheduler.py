import asyncio
import logging
import time

from .github import GitHubAPI
from .k8s import KubernetesAPI, runner_pod_spec

logger = logging.getLogger("ruyici.scheduler")


class State:
    def __init__(self):
        self.jobs = {}
        self.workers = {}


class RunnerService:
    """Owns in-memory job/worker state and drives webhooks + reconciliation."""

    def __init__(self, config):
        self.cfg = config
        self.gh = GitHubAPI(config)
        self.k8s = KubernetesAPI(config)
        self.state = State()
        self._lock = asyncio.Lock()

    def _label_matches(self, labels):
        return len(labels) == 1 and labels[0] == self.cfg.runner_label

    async def handle_event(self, gh_event, payload):
        if gh_event == "ping":
            logger.info("ping received")
            return
        if gh_event != "workflow_job":
            logger.debug("event %s ignored", gh_event)
            return
        action = payload.get("action")
        job = payload["workflow_job"]
        job_id = str(job["id"])
        async with self._lock:
            s = self.state.jobs.get(job_id)
            if action == "queued":
                if not self._label_matches(job.get("labels", [])):
                    logger.info("job %s ignored (labels=%s)", job_id, job.get("labels"))
                    return
                if s is None:
                    self.state.jobs[job_id] = {
                        "id": job_id,
                        "name": job.get("name", ""),
                        "repo": payload["repository"]["full_name"],
                        "labels": list(job.get("labels", [])),
                        "status": "pending",
                        "created_at": time.time(),
                        "started_at": None,
                        "completed_at": None,
                        "conclusion": None,
                    }
                    logger.info(
                        "job %s queued from %s",
                        job_id,
                        payload["repository"]["full_name"],
                    )
            elif action == "in_progress":
                if s and s["status"] == "pending":
                    s["status"] = "running"
                    s["started_at"] = s["started_at"] or time.time()
            elif action == "completed":
                if s:
                    s["started_at"] = s["started_at"] or time.time()
                    s["completed_at"] = s["completed_at"] or time.time()
                    s["status"] = "completed"
                    s["conclusion"] = job.get("conclusion") or "completed"

    async def reconcile(self):
        async with self._lock:
            await self._sync_jobs()
            await self._sync_workers()
            await self._cleanup_terminal()
            await self._demand_match()

    async def _sync_jobs(self):
        now = time.time()
        for j in self.state.jobs.values():
            if j["status"] != "pending":
                continue
            if now - j["created_at"] < self.cfg.stuck_queued_min_age:
                continue
            info = await self.gh.get_job(j["repo"], j["id"])
            if info is None:
                logger.warning("job %s gone on GitHub, marking failed", j["id"])
                j["status"] = "failed"
                j["conclusion"] = "failed"
                j["completed_at"] = now
            elif info.get("status") == "completed":
                logger.warning(
                    "job %s completed while unprovisioned, marking failed", j["id"]
                )
                j["status"] = "failed"
                j["conclusion"] = "stuck_queued"
                j["completed_at"] = now

    async def _sync_workers(self):
        now = time.time()
        runner_rows = None
        for name, w in list(self.state.workers.items()):
            if w["status"] not in ("pending", "running"):
                continue
            pod = await self.k8s.get_pod(name)
            if pod is None:
                if now - w["created_at"] > 90:
                    w["status"] = "failed"
                    w["completed_at"] = now
                    w["failure_info"] = "worker has no matching pod (orphan)"
                    logger.warning("worker %s has no pod", name)
                continue
            phase = pod["status"].get("phase", "Pending")
            w["pod_phase"] = phase
            if phase == "Succeeded":
                w["status"] = "completed"
                w["completed_at"] = w["completed_at"] or now
                continue
            if phase == "Failed":
                w["status"] = "failed"
                w["completed_at"] = w["completed_at"] or now
                w["failure_info"] = "pod failed"
                await self.k8s.delete_pod(name)
                continue
            if phase == "Pending":
                if now - w["created_at"] > self.cfg.pod_pending_timeout:
                    w["status"] = "failed"
                    w["completed_at"] = now
                    w["failure_info"] = "pod stuck pending"
                    await self.k8s.delete_pod(name)
                continue

            if runner_rows is None:
                runner_rows = await self.gh.list_runners()
            rr = next((r for r in runner_rows if r.get("name") == name), None)
            w["registered"] = rr is not None
            if rr is None:
                if now - w["created_at"] > self.cfg.registration_timeout:
                    w["status"] = "failed"
                    w["completed_at"] = now
                    w["failure_info"] = "runner never registered on GitHub"
                    await self.k8s.delete_pod(name)
                continue
            w["gh_runner_id"] = rr["id"]
            if (
                not rr.get("busy")
                and now - w["created_at"] > self.cfg.runner_idle_timeout
            ):
                w["status"] = "failed"
                w["completed_at"] = now
                w["failure_info"] = "runner idle and never picked up the job"
                await self.k8s.delete_pod(name)
                try:
                    await self.gh.delete_runner(rr["id"])
                except Exception:
                    logger.exception("failed to delete GitHub runner %s", rr["id"])

    async def _cleanup_terminal(self):
        now = time.time()
        for name, w in list(self.state.workers.items()):
            if w["status"] not in ("completed", "failed"):
                continue
            if (
                w.get("completed_at")
                and now - w["completed_at"] < self.cfg.pod_delete_grace
            ):
                continue
            if w.get("gh_runner_id"):
                try:
                    await self.gh.delete_runner(w["gh_runner_id"])
                except Exception:
                    logger.exception(
                        "failed to delete GitHub runner %s", w["gh_runner_id"]
                    )
            try:
                await self.k8s.delete_pod(name)
            except Exception:
                logger.exception("failed to delete pod %s", name)
            del self.state.workers[name]

    async def _demand_match(self):
        active_jobs = [
            j for j in self.state.jobs.values() if j["status"] in ("pending", "running")
        ]
        active_jobs.sort(key=lambda j: (j["created_at"], j["id"]))
        # Capacity ceiling = available labelled nodes * runners per node. With
        # per-node packing the default (1) mirrors RISE (one runner per node).
        nodes = await self.k8s.list_labelled_nodes()
        capacity = len(nodes) * self.cfg.runners_per_node
        for j in active_jobs:
            matching_demand = sum(
                1
                for x in self.state.jobs.values()
                if x["status"] in ("pending", "running") and x["labels"] == j["labels"]
            )
            matching_supply = sum(
                1
                for w in self.state.workers.values()
                if w["status"] in ("pending", "running") and w["labels"] == j["labels"]
            )
            if matching_supply >= matching_demand:
                continue
            active_workers = sum(
                1
                for w in self.state.workers.values()
                if w["status"] in ("pending", "running")
            )
            if active_workers >= self.cfg.max_workers:
                logger.info("max_workers cap %s reached", self.cfg.max_workers)
                break
            if capacity and active_workers >= capacity:
                logger.info(
                    "node capacity exhausted (%s runners across %s nodes)",
                    capacity,
                    len(nodes),
                )
                break
            name = f"{self.cfg.runner_prefix}{j['id']}"
            if name in self.state.workers:
                continue
            try:
                jit = await self.gh.create_jit_config(name, [self.cfg.runner_label])
            except Exception:
                logger.exception(
                    "JIT config failed for %s; will retry next cycle", name
                )
                continue
            jit_config = jit["encoded_jit_config"] if isinstance(jit, dict) else jit
            self.state.workers[name] = {
                "name": name,
                "job_id": j["id"],
                "labels": list(j["labels"]),
                "status": "pending",
                "created_at": time.time(),
                "completed_at": None,
                "pod_phase": None,
                "registered": False,
                "gh_runner_id": None,
                "failure_info": None,
            }
            await self.k8s.create_pod(runner_pod_spec(self.cfg, name, jit_config))
            logger.info("provisioned pod %s for job %s", name, j["id"])


async def scheduler_loop(service):
    while True:
        try:
            await service.reconcile()
        except Exception:
            logger.exception("reconcile failed")
        await asyncio.sleep(service.cfg.poll_interval)
