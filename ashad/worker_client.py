"""Backend-only client for explicitly bound desktop workers.

Never retry an ambiguous input POST. Reconcile using action_status(action_id).
The token belongs in the coordinator's credential store, never agent tool context.
"""
from __future__ import annotations

from typing import Any

import httpx


class WorkerError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, ambiguous: bool = False,
                 *, method: str | None = None, path: str | None = None, code: str | None = None):
        operation = f"{method} {path.split('?')[0]}" if method and path else "Worker request"
        status = f" (HTTP {status_code})" if status_code is not None else ""
        super().__init__(f"{operation}{status}: {message}")
        self.status_code = status_code
        self.ambiguous = ambiguous
        self.method, self.path, self.code = method, path, code


class WorkerClient:
    def __init__(self, base_url: str, token: str, timeout: float = 15.0, *, transport=None):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(base_url=self.base_url, headers={"Authorization": f"Bearer {token}"},
            timeout=timeout, transport=transport, trust_env=False)
        self.binding: dict[str, str] = {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def request(self, method: str, path: str, *, data=None, params=None, raw=False, timeout=None) -> Any:
        try:
            response = await self._client.request(method, path, json=data, params=params, timeout=timeout or self._client.timeout)
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            try:
                data = exc.response.json()
                detail = data.get("detail", data) if isinstance(data, dict) else data
            except ValueError:
                detail = exc.response.text[:500]
            code = detail.get("code") if isinstance(detail, dict) else None
            message = detail.get("message", detail) if isinstance(detail, dict) else detail
            raise WorkerError(str(message)[:1000], exc.response.status_code,
                ambiguous=method == "POST" and exc.response.status_code >= 500,
                method=method, path=path, code=code) from exc
        except httpx.RequestError as exc:
            raise WorkerError(f"Desktop worker unavailable: {type(exc).__name__}", ambiguous=method == "POST", method=method, path=path) from exc
        if raw:
            return response.content
        try:
            return response.json()
        except ValueError as exc:
            raise WorkerError("Worker returned invalid JSON", response.status_code, ambiguous=method == "POST", method=method, path=path) from exc

    def set_binding(self, run_id: str, player_id: str, epoch: str) -> "WorkerClient":
        self.binding = {"run_id": run_id, "player_id": player_id, "epoch": epoch}
        return self

    def _bound(self, **values) -> dict:
        if not self.binding:
            raise WorkerError("Set a run/player/epoch binding before issuing worker commands")
        return {**self.binding, **values}

    async def health(self) -> dict:
        return await self.request("GET", "/health")

    async def status(self) -> dict:
        return await self.request("GET", "/status")

    async def bind(self, run_id: str, player_id: str, epoch: str, identity: str,
                   transport: str = "enet", app_id: int | None = None, settings: dict | None = None) -> dict:
        result = await self.request("POST", "/bind", data=dict(run_id=run_id, player_id=player_id, epoch=epoch,
            identity=identity, transport=transport, app_id=app_id, settings=settings or {}))
        self.set_binding(run_id, player_id, epoch)
        return result

    async def launch(self, command: list[str], env: dict[str, str] | None = None,
                     command_id: str | None = None) -> dict:
        return await self.request("POST", "/launch", data=self._bound(command=command, env=env or {},
            command_id=command_id))

    async def heartbeat(self, investigation_deadline=None, run_deadline=None) -> dict:
        return await self.request("POST", "/heartbeat", data=self._bound(
            investigation_deadline=investigation_deadline, run_deadline=run_deadline))

    async def pause(self, command_id: str | None = None) -> dict:
        return await self.request("POST", "/pause", data=self._bound(command_id=command_id))

    async def resume(self, command_id: str | None = None) -> dict:
        return await self.request("POST", "/resume", data=self._bound(command_id=command_id))

    async def stop(self, terminate: bool = False, reason: str = "requested", command_id: str | None = None) -> dict:
        return await self.request("POST", "/stop", data=self._bound(terminate=terminate, reason=reason, command_id=command_id))

    async def disconnect(self, reason: str = "scheduled_disconnect") -> dict:
        return await self.request("POST", "/disconnect", data=self._bound(terminate=False, reason=reason))

    async def observation(self, fresh: bool = False) -> dict:
        result = await self.request("GET", "/observation", params={"fresh": str(fresh).lower()})
        return result

    async def screenshot(self, observation_id: str | None = None) -> bytes:
        return await self.request("GET", "/frame.jpg", params={"observation_id": observation_id} if observation_id else None, raw=True)

    async def live_stream(self, run_id: str, player_id: str, epoch: str):
        request = self._client.build_request("GET", "/live.mjpeg",
            params={"run_id": run_id, "player_id": player_id, "epoch": epoch})
        response = await self._client.send(request, stream=True)
        if response.is_error:
            await response.aclose()
            raise WorkerError("Live view unavailable", response.status_code, method="GET", path="/live.mjpeg")
        return response



    async def telemetry(self) -> dict:
        """Monitor capability. Do not expose this method to a player agent's tools."""
        return await self.request("GET", "/telemetry")

    async def pin(self, incident_id: str, before_seconds: int = 30, after_seconds: int = 30) -> dict:
        return await self.request("POST", "/pin", data=self._bound(incident_id=incident_id, before_seconds=before_seconds, after_seconds=after_seconds))

    async def evidence(self, path: str, run_id: str | None = None) -> bytes:
        return await self.request("GET", "/evidence", params={"path": path, **({"run_id": run_id} if run_id else {})}, raw=True)

    async def evidence_index(self, run_id: str | None = None) -> dict:
        return await self.request("GET", "/evidence/index", params={"run_id": run_id} if run_id else None)

    async def fixture_export(self, manifest: dict) -> dict:
        return await self.request("POST", "/fixtures/export", data=manifest)

    async def fixture_import(self, manifest: dict, archive_base64: str, identity_mapping: dict | None = None) -> dict:
        return await self.request("POST", "/fixtures/import", data=self._bound(manifest=manifest,
            archive_base64=archive_base64, identity_mapping=identity_mapping or {}))

    async def steam_setup_start(self) -> dict:
        return await self.request("POST", "/setup/steam/start")

    async def steam_setup_finish(self) -> dict:
        return await self.request("POST", "/setup/steam/finish")

    async def steam_verify(self, app_id: int, steam_id: str, command: list[str] | None = None) -> dict:
        data = {"app_id": app_id, "steam_id": steam_id}
        if command is not None:
            data["command"] = command
        return await self.request("POST", "/setup/steam/verify", data=data, timeout=30)
