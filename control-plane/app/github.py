import logging
import time

import httpx
import jwt

logger = logging.getLogger("ruyici.github")


class GitHubAPI:
    """GitHub App authentication and Actions runner management."""

    def __init__(self, config):
        self.cfg = config
        self._client = None
        self._install_token = None
        self._token_expires = 0.0
        self._base_headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

    @property
    def client(self):
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.cfg.gh_api, timeout=30.0)
        return self._client

    def _jwt(self, ttl=540):
        now = int(time.time())
        return jwt.encode(
            {"iat": now, "exp": now + ttl, "iss": self.cfg.gh_app_id},
            self.cfg.gh_private_key_pem,
            algorithm="RS256",
        )

    async def _request(self, method, url, *, headers=None, **kwargs):
        h = dict(self._base_headers)
        if headers:
            h.update(headers)
        resp = await self.client.request(method, url, headers=h, **kwargs)
        if resp.status_code == 404 and method in ("GET", "DELETE"):
            return None
        resp.raise_for_status()
        if resp.status_code == 204:
            return None
        return resp.json()

    async def install_token(self):
        now = time.time()
        if self._install_token and now < self._token_expires - 60:
            return self._install_token
        data = await self._request(
            "GET",
            f"/orgs/{self.cfg.gh_org}/installation",
            headers={"Authorization": f"Bearer {self._jwt()}"},
        )
        self._install_token = data["token"]
        self._token_expires = now + 3600
        return self._install_token

    async def _auth_headers(self):
        return {"Authorization": f"Bearer {await self.install_token()}"}

    async def ensure_runner_group(self):
        headers = await self._auth_headers()
        data = await self._request(
            "GET", f"/orgs/{self.cfg.gh_org}/actions/runner-groups", headers=headers
        )
        for group in data.get("runner_groups", []):
            if group.get("name") == self.cfg.runner_group:
                return group["id"]
        created = await self._request(
            "POST",
            f"/orgs/{self.cfg.gh_org}/actions/runner-groups",
            headers=headers,
            json={"name": self.cfg.runner_group},
        )
        logger.info(
            "created runner group %s (id=%s)", self.cfg.runner_group, created["id"]
        )
        return created["id"]

    async def create_jit_config(self, runner_name, labels):
        headers = await self._auth_headers()
        group_id = await self.ensure_runner_group()
        return await self._request(
            "POST",
            f"/orgs/{self.cfg.gh_org}/actions/runners/generate-jitconfig",
            headers=headers,
            json={"name": runner_name, "runner_group_id": group_id, "labels": labels},
        )

    async def get_job(self, repo_full_name, job_id):
        headers = await self._auth_headers()
        return await self._request(
            "GET", f"/repos/{repo_full_name}/actions/jobs/{job_id}", headers=headers
        )

    async def list_runners(self):
        headers = await self._auth_headers()
        runners = []
        for page in range(1, 11):
            data = await self._request(
                "GET",
                f"/orgs/{self.cfg.gh_org}/actions/runners?per_page=100&page={page}",
                headers=headers,
            )
            items = data.get("runners", [])
            runners.extend(items)
            if len(items) < 100:
                break
        return runners

    async def delete_runner(self, runner_id):
        headers = await self._auth_headers()
        return await self._request(
            "DELETE",
            f"/orgs/{self.cfg.gh_org}/actions/runners/{runner_id}",
            headers=headers,
        )
