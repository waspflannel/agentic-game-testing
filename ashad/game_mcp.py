"""Local MCP facade; explicit short-lived sessions bind one run/player/control owner."""
from __future__ import annotations

import secrets
import time
from typing import Any

from mcp.server.fastmcp import Context, FastMCP


class GameMCP:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.sessions = {}
        self.server = FastMCP("ASHAD game", stateless_http=True, json_response=True, streamable_http_path="/")
        self.app = self.server.streamable_http_app()
        server = self.server

        async def invoke(ctx, name, arguments):
            session = self.resolve(ctx.request_context.request.headers.get("authorization", ""))
            run_id, player_id = session["run_id"], session["player_id"]
            service = coordinator.structured
            mutating = name in {"perform_action", "cancel_action", "wait_for_change"}
            if mutating and not session["control"]:
                raise ValueError("This MCP session is observation-only")
            observation = await service.observe(run_id, player_id)
            result = observation
            if name != "observe":
                result = await service.dispatch(
                    run_id, player_id, name, arguments, observation,
                    session["generation"], owner=session["owner"],
                )
            coordinator.store.event(run_id, "mcp_tool_result", {"player_id": player_id, "tool": name, "result": result})
            return result

        @server.tool()
        async def observe(ctx: Context) -> dict[str, Any]:
            """Read this session's filtered player observation."""
            return await invoke(ctx, "observe", {})

        @server.tool()
        async def inspect_entity(target_id: str, ctx: Context) -> dict[str, Any]:
            """Inspect a currently observed object or UI control."""
            return await invoke(ctx, "inspect_entity", {"target_id": target_id})

        @server.tool()
        async def list_actions(target_id: str, ctx: Context) -> dict[str, Any]:
            """List advertised actions for a visible target."""
            return await invoke(ctx, "list_actions", {"target_id": target_id})

        @server.tool()
        async def list_entities(ctx: Context, query: str = "", offset: int = 0, limit: int = 40) -> dict[str, Any]:
            """Page filtered visible targets without exposing private or hidden objects."""
            return await invoke(ctx, "list_entities", {"query": query, "offset": offset, "limit": limit})

        @server.tool()
        async def explain_objective(ctx: Context) -> dict[str, Any]:
            """Read goals and scenario-approved guidance."""
            return await invoke(ctx, "explain_objective", {})

        @server.tool()
        async def perform_action(verb: str, parameters: dict, reason: str, ctx: Context,
                                 target_id: str = "", target_version: int = -1) -> dict[str, Any]:
            """Send a bounded normal game action. Requires an explicitly issued control session."""
            return await invoke(ctx, "perform_action", {"verb": verb, "parameters": parameters,
                "reason": reason, "target_id": target_id, "target_version": target_version})

        @server.tool()
        async def get_action_status(action_id: str, ctx: Context) -> dict[str, Any]:
            """Reconcile execution before retrying any uncertain action."""
            return await invoke(ctx, "get_action_status", {"action_id": action_id})

        @server.tool()
        async def cancel_action(action_id: str, ctx: Context) -> dict[str, Any]:
            """Cancel input; already committed game effects cannot be undone."""
            return await invoke(ctx, "cancel_action", {"action_id": action_id})

        @server.tool()
        async def read_changes(after: int, generation: str, ctx: Context) -> dict[str, Any]:
            """Read bounded deltas; observe again when resync_required is true."""
            return await invoke(ctx, "read_changes", {"after": after, "generation": generation})

        @server.resource("game://player/observation")
        async def observation_resource(ctx: Context) -> dict[str, Any]:
            return await invoke(ctx, "observe", {})

    def cleanup(self):
        for token, session in list(self.sessions.items()):
            run = self.coordinator.store.get("runs", session["run_id"], {})
            ps = run.get("players", {}).get(session["player_id"], {})
            if time.time() >= session["expires_at"] or run.get("state") not in {"Starting", "Running", "Paused", "Investigating"} or ps.get("generation") != session["generation"]:
                key = (session["run_id"], session["player_id"])
                self.coordinator.structured.release_control(key, session["owner"])
                self.sessions.pop(token, None)

    def issue(self, run_id, player_id, control=False):
        self.cleanup()
        _, ps = self.coordinator.structured.binding(run_id, player_id)
        key = (run_id, player_id)
        owner = secrets.token_hex(12)
        if control:
            self.coordinator.structured.claim_control(key, owner)
        token = secrets.token_urlsafe(32)
        self.sessions[token] = {"run_id": run_id, "player_id": player_id, "control": bool(control),
            "generation": ps["generation"], "owner": owner, "expires_at": time.time() + 60}
        return {"token": token, "expires_in_seconds": 60, "control": bool(control), "path": "/mcp/"}

    def resolve(self, authorization):
        self.cleanup()
        session = self.sessions.get(authorization.removeprefix("Bearer ")) if authorization.startswith("Bearer ") else None
        if not session:
            raise ValueError("A current player-bound MCP session is required")
        session["expires_at"] = time.time() + 60
        return session
