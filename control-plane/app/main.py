import asyncio
import json
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .config import Config
from .scheduler import RunnerService, scheduler_loop
from .webhook import verify_signature

logging.basicConfig(level=logging.INFO)


def create_app(cfg=None, service=None):
    cfg = cfg or Config()
    service = service or RunnerService(cfg)

    @asynccontextmanager
    async def lifespan(_app):
        task = asyncio.create_task(scheduler_loop(service))
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    app = FastAPI(
        title="ruyici runner service", lifespan=lifespan, docs_url=None, redoc_url=None
    )

    @app.post("/")
    async def webhook(request: Request):
        body = await request.body()
        sig = request.headers.get("x-hub-signature-256", "")
        if not verify_signature(cfg.gh_webhook_secret, body, sig):
            return JSONResponse(status_code=401, content={"error": "invalid signature"})
        event = request.headers.get("x-github-event", "")
        try:
            payload = json.loads(body)
        except ValueError:
            return JSONResponse(status_code=400, content={"error": "invalid json"})
        await service.handle_event(event, payload)
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/jobs")
    async def jobs():
        return {
            "total_count": len(service.state.jobs),
            "jobs": list(service.state.jobs.values()),
        }

    @app.get("/workers")
    async def workers():
        return {
            "total_count": len(service.state.workers),
            "workers": list(service.state.workers.values()),
        }

    @app.get("/usage")
    async def usage():
        active_workers = [
            w
            for w in service.state.workers.values()
            if w["status"] in ("pending", "running")
        ]
        demand = sum(
            1
            for j in service.state.jobs.values()
            if j["status"] in ("pending", "running")
        )
        return {
            "demand": demand,
            "supply": len(active_workers),
            "max_workers": cfg.max_workers,
        }

    return app


app = create_app()
