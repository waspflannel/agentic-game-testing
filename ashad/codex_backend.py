"""Subscription-only Codex selections through the official local SDK."""
import asyncio
from contextlib import suppress
import json
import logging
import time

from openai_codex import AsyncCodex, ApprovalMode, CodexConfig, Sandbox
from openai_codex.types import ReasoningEffort
from openai_codex.generated.v2_all import ThreadUnsubscribeResponse

from .models import MODEL, CODEX_MODELS
from .prompts import TUNING_MANUAL_LIMIT, TUNING_PROMPT_VERSION, planning_request, selection_request, tuning_request
from .store import redact


READY_TTL_SECONDS = 30  # Reuse a positive account/model check across consecutive turns.


class CodexError(Exception):
    def __init__(self, status, message, *, selection=None):
        self.status, self.message, self.selection = status, message, selection
        super().__init__(message)


def provider_error(exc):
    message = redact(str(exc))[:1200]
    lowered = message.lower()
    status = "provider_failure"
    if any(word in lowered for word in ("usage limit", "rate limit", "quota", "429")):
        status = "rate_limited"
    elif any(word in lowered for word in ("unauthorized", "not logged in", "authentication", "refresh token", "401")):
        status = "invalid_authentication"
    elif "model" in lowered and any(word in lowered for word in ("not found", "not available", "not supported", "access")):
        status = "model_unavailable"
    return CodexError(status, f"Codex: {message}")


class CodexBackend:
    def __init__(self, store, *, client=None):
        self.home = store.root / "codex"
        self.workspace = self.home / "selection-workspace"
        self.client = client
        self.login = None
        self.login_wait = None
        self.login_error = None
        self.auth_lock = asyncio.Lock()
        self.ready_until = {}  # model -> monotonic deadline of the last positive status()

    def runtime(self):
        if self.client is None:
            self.workspace.mkdir(parents=True, exist_ok=True)
            # Keep personal coding instructions/tools and API credentials out of gameplay.
            config = CodexConfig(cwd=str(self.workspace), client_name="ashad_bot",
                client_title="ASHAD gameplay", env={"CODEX_HOME": str(self.home),
                    "OPENAI_API_KEY": "", "CODEX_API_KEY": "", "CODEX_ACCESS_TOKEN": "", "OPENAI_BASE_URL": ""},
                config_overrides=(
                    'forced_login_method="chatgpt"', 'cli_auth_credentials_store="keyring"',
                    'model_provider="openai"', 'web_search="disabled"',
                    'features.shell_tool=false', 'features.unified_exec=false',
                    'features.code_mode.enabled=false', 'features.apps=false',
                    'features.multi_agent=false', 'features.remote_plugin=false',
                    'features.memories=false', 'features.hooks=false',
                    'features.goals=false', 'features.skill_mcp_dependency_install=false',
                    'tools.view_image=false', 'skills.max_context_tokens=1', 'project_doc_max_bytes=0',
                ))
            self.client = AsyncCodex(config)
        return self.client

    async def status(self, model=MODEL):
        result = {"configured": False, "status": "configuration_required", "model": model, "available_models": [],
            "api": "codex", "auth_method": "chatgpt", "billing": "subscription",
            "message": "Sign in with ChatGPT in AI Settings to use your Codex subscription."}
        if model not in CODEX_MODELS:
            result.update(status="model_unavailable", message=f"Unsupported Codex model: {model}.")
            return result
        self.ready_until.pop(model, None)
        if self.login_wait and not self.login_wait.done():
            result.update(status="login_pending", message="Complete ChatGPT sign-in in your browser, then refresh status.")
            return result
        try:
            async with asyncio.timeout(25):
                client = self.runtime()
                account = (await client.account()).account
                account = account.root if account else None
                if account is None:
                    if self.login_error:
                        result.update(status="invalid_authentication", message=self.login_error)
                    return result
                if account.type != "chatgpt":
                    result.update(status="invalid_authentication", message="ASHAD requires ChatGPT subscription sign-in. API-key billing is disabled.")
                    return result
                result.update(configured=True, plan_type=str(getattr(account.plan_type, "value", account.plan_type)))
                models = await client.models()
                available = {item.model for item in models.data}
                result["available_models"] = [name for name in CODEX_MODELS if name in available]
                if model not in available:
                    result.update(status="model_unavailable", message=f"Your Codex account does not list {model}. No substitute model or API billing was used.")
                    return result
                result.update(status="ready", message=f"ChatGPT subscription connected; {model} is available in Codex. Account usage limits apply.")
                self.ready_until[model] = time.monotonic() + READY_TTL_SECONDS
        except TimeoutError:
            result.update(status="connection_failure", message="Codex did not respond to the sign-in check within 25 seconds.")
        except Exception as exc:
            error = provider_error(exc)
            result.update(status=error.status, message=error.message)
        return result

    async def login_start(self):
        async with self.auth_lock:
            self.ready_until.clear()
            if self.login_wait and not self.login_wait.done():
                return {"auth_url": self.login.auth_url, "login_id": self.login.login_id}
            try:
                self.login = await asyncio.wait_for(self.runtime().login_chatgpt(), 25)
            except Exception as exc:
                raise provider_error(exc) from None
            self.login_error = None
            self.login_wait = asyncio.create_task(self._wait_login(self.login))
            return {"auth_url": self.login.auth_url, "login_id": self.login.login_id}

    async def _wait_login(self, handle):
        try:
            completed = await handle.wait()
            if not completed.success:
                self.login_error = "ChatGPT sign-in did not complete. Start sign-in again."
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.login_error = provider_error(exc).message

    async def login_cancel(self):
        async with self.auth_lock:
            self.ready_until.clear()
            try:
                if self.login_wait and not self.login_wait.done():
                    await asyncio.wait_for(self.login.cancel(), 5)
            except Exception as exc:
                raise provider_error(exc) from None
            finally:
                if self.login_wait and not self.login_wait.done():
                    self.login_wait.cancel()
                    await asyncio.gather(self.login_wait, return_exceptions=True)
                self.login = self.login_wait = None
                self.login_error = None

    async def logout(self):
        await self.login_cancel()  # Also clears cached readiness.
        try:
            await asyncio.wait_for(self.runtime().logout(), 10)
        except Exception as exc:
            raise provider_error(exc) from None

    async def select(self, input, questions, *, model=MODEL):
        payload, schema, instructions = selection_request(input, questions)
        return await self._complete(payload, schema, instructions, model)

    async def plan(self, evidence, schema, *, model=MODEL):
        payload, schema, instructions = planning_request(evidence, schema)
        return await self._complete(payload, schema, instructions, model, operation="planning")

    async def tune(self, evidence, *, model=MODEL, stage="manual", repair=False):
        payload, schema, instructions = tuning_request(evidence, stage, repair)
        return await self._complete(payload, schema, instructions, model, operation="tuning")

    async def _complete(self, payload, schema, instructions, model, *, operation="selection"):
        if self.ready_until.get(model, 0) <= time.monotonic():
            status = await self.status(model)
            if status["status"] != "ready":
                raise CodexError(status["status"], status["message"])
        client = self.runtime()
        thread = handle = starting = None
        try:
            # Synthesizing a whole controls manual takes longer than choosing an action.
            async with asyncio.timeout(300 if operation == "tuning" else 120):
                thread = await client.thread_start(model=model, model_provider="openai",
                    ephemeral=True, cwd=str(self.workspace), approval_mode=ApprovalMode.deny_all,
                    sandbox=Sandbox.read_only, base_instructions=instructions)
                starting = asyncio.create_task(thread.turn(json.dumps(payload, ensure_ascii=False),
                    model=model, output_schema=schema, effort=ReasoningEffort.medium if operation in {"tuning", "planning"} else ReasoningEffort.low,
                    approval_mode=ApprovalMode.deny_all, sandbox=Sandbox.read_only))
                handle = await asyncio.shield(starting)
                result = await handle.run()
                # Each request owns a fresh thread, so cumulative usage covers all internal requests.
                usage = result.usage.total.model_dump() if result.usage else None
                selection = {"thread_id": thread.id, "turn_id": result.id, "usage": usage, "usage_scope": operation + "_total"}
                if getattr(result.status, "value", result.status) != "completed":
                    raise CodexError("incomplete_response", f"Codex did not complete {operation}; no action was executed.", selection=selection)
                try:
                    answers = json.loads(result.final_response or "")
                except ValueError:
                    raise CodexError("invalid_plan" if operation == "planning" else "invalid_decision",
                        f"Codex returned invalid {operation} JSON; no action was executed.", selection=selection) from None
                return {"answers": answers, **selection}
        except (asyncio.CancelledError, TimeoutError) as exc:
            # A cancelled Python await does not stop the server's model turn.
            try:
                if handle is None and starting is not None:
                    handle = await asyncio.wait_for(asyncio.shield(starting), 5)
                if self.client is not client:
                    pass  # Another cancelled selection already closed this runtime.
                elif handle:
                    await asyncio.wait_for(handle.interrupt(), 5)
                else:
                    await self.close()
            except Exception:
                await self.close()
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise CodexError("connection_failure", f"Codex {operation} timed out and was interrupted; no game action was executed.") from None
        except CodexError:
            raise
        except Exception as exc:
            self.ready_until.clear()  # Re-check sign-in/model before the next turn.
            raise provider_error(exc) from None
        finally:
            if thread and self.client is client:
                try:
                    # Pinned SDK exposes this documented RPC through its typed client;
                    # archiving an ephemeral thread does not reliably unload it.
                    await asyncio.wait_for(client._client.request("thread/unsubscribe",
                        {"threadId": thread.id}, response_model=ThreadUnsubscribeResponse), 5)
                except Exception:
                    await self.close()

    async def close(self):
        try:
            with suppress(CodexError):
                await self.login_cancel()
        finally:
            if self.client:
                client, self.client = self.client, None
                try:
                    await asyncio.wait_for(client.close(), 5)
                except Exception as exc:
                    # Cleanup must preserve the original provider failure or cancellation.
                    logging.getLogger(__name__).warning("Codex cleanup failed: %s", redact(str(exc)))
