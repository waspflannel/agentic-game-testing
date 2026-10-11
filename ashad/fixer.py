"""Improvement loop: fixes unsupported actions in ASHAD's agent layer through jr, one PR per issue, then verifies from the part 1 save before the failure. Crashes and game bugs are never fixed here. Every external command goes through Fixer.runner."""
import asyncio
from contextlib import suppress
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import time

import keyring.errors

from .claude_backend import TOKEN_REFERENCE
from .store import uid


LOG = logging.getLogger(__name__)
FIX_REPO = "waspflannel/agentic-game-testing"
BASE_BRANCH = "main"
FIXER_ARGV = ("jr", "ship")
ALLOWED_PATHS = frozenset({"ashad/game_mcp.py", "ashad/structured.py", "ashad/ai.py", "ashad/prompts.py"})
TEST_PATH = re.compile(r"tests/test_[a-z0-9_]+\.py")
PROTECTED_TESTS = frozenset({"tests/conftest.py", "tests/test_fixer.py", "tests/test_unsupported_actions.py",
                             "tests/test_crash_bug_issues.py", "tests/test_instance_issues.py", "tests/test_saves.py"})
MAX_ATTEMPTS_PER_ISSUE, MAX_ATTEMPTS_PER_LOOP, MAX_FIXES_PER_LOOP, MAX_CONSECUTIVE_FAILURES = 3, 8, 5, 3
FIXER_TIMEOUT_SECONDS, GATE_TIMEOUT_SECONDS, COMMAND_TIMEOUT_SECONDS = 3600, 1200, 120
VERIFY_DECISIONS, VERIFY_RUNNING_SECONDS, WATCH_INTERVAL_SECONDS = 30, 900, 2
STATUS_CACHE_SECONDS, KEEP_ATTEMPT_DIRS, EVIDENCE_CHARS, LOG_TAIL_BYTES = 60, 30, 2000, 65536
REGRESSION_CATEGORIES = frozenset({"structured_interface_failure", "controller_failure"})
USAGE_LIMIT = re.compile(r"(?i)usage limit|rate limit|\b429\b")
FIXER_ENV_KEYS = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TMPDIR", "XDG_RUNTIME_DIR",
                  "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "DBUS_SESSION_BUS_ADDRESS")
REPO_URLS = ("https://github.com/waspflannel/agentic-game-testing", "https://github.com/waspflannel/agentic-game-testing.git",
             "git@github.com:waspflannel/agentic-game-testing.git", "ssh://git@github.com/waspflannel/agentic-game-testing.git")
RUNTIME_ROOT = Path(__file__).resolve().parents[1]
ACTIVE_PHASES = frozenset({"fixing", "gating", "merging", "applying", "reverting"})
WRITE_PERMISSIONS = frozenset({"WRITE", "MAINTAIN", "ADMIN"})
DEFAULT_DAILY_MINUTES = 240
KILL_GRACE_SECONDS = 10


def _log_tail(path):
    with open(path, "rb") as stream:
        stream.seek(0, os.SEEK_END)
        stream.seek(max(0, stream.tell() - LOG_TAIL_BYTES))
        return stream.read().decode("utf-8", "replace")


def _signal_group(pid, sig):
    with suppress(ProcessLookupError):
        os.killpg(pid, sig)


async def _stop_group(proc):
    _signal_group(proc.pid, signal.SIGTERM)
    try:
        await asyncio.wait_for(proc.wait(), KILL_GRACE_SECONDS)
    except asyncio.TimeoutError:
        pass
    _signal_group(proc.pid, signal.SIGKILL)
    await proc.wait()


async def _drain(stream, buffer):
    while chunk := await stream.read(8192):
        buffer.extend(chunk)
        del buffer[:-LOG_TAIL_BYTES]


async def run_command(argv, *, cwd, env, timeout, log_path=None):
    """Run argv in its own process group; (return code, output tail), or (None, tail) after a timeout."""
    log = open(log_path, "ab") if log_path else None
    try:
        proc = await asyncio.create_subprocess_exec(*argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                                    stdout=log or subprocess.PIPE, stderr=subprocess.STDOUT,
                                                    start_new_session=True)
        buffer = bytearray()
        reader = None if log else asyncio.create_task(_drain(proc.stdout, buffer))
        try:
            await asyncio.wait_for(proc.wait(), timeout)
            code = proc.returncode
        except (asyncio.TimeoutError, asyncio.CancelledError) as error:
            await _stop_group(proc)
            if isinstance(error, asyncio.CancelledError):
                raise
            code = None
        finally:
            if reader:
                with suppress(asyncio.TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(reader, 5)
                reader.cancel()
        if log:
            log.flush()
            return code, _log_tail(log_path)
        return code, bytes(buffer).decode("utf-8", "replace")
    finally:
        if log:
            log.close()


DEFAULT_RUNNER = run_command


def jr_branch(attempt_id):
    return "build/" + attempt_id.replace("_", "-")


def fixer_env(token=None):
    """An explicit allowlist, never the whole environment; only jr gets the Claude token."""
    env = {key: os.environ[key] for key in FIXER_ENV_KEYS if os.environ.get(key)}
    if token:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    return env


class Fixer:
    def __init__(self, coordinator, runner=None):
        self.coordinator, self.store = coordinator, coordinator.store
        self.runner = runner or DEFAULT_RUNNER
        self.which = shutil.which
        self.checkout_root = None
        self.tasks = {}
        self._status = (0.0, None)

    async def _run(self, argv, *, cwd, timeout=COMMAND_TIMEOUT_SECONDS, log_path=None, token=None):
        return await self.runner(list(argv), cwd=str(cwd), env=fixer_env(token), timeout=timeout, log_path=log_path)

    def _settings_minutes(self):
        return (self.store.get("settings", "main") or {}).get("fixer_daily_minutes", DEFAULT_DAILY_MINUTES)

    def _usage_key(self):
        return "fixer_usage_" + datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def minutes_today(self):
        return (self.store.get("fixer_usage", self._usage_key()) or {}).get("minutes", 0)

    def _add_minutes(self, minutes):
        key = self._usage_key()
        with self.store.transaction():
            usage = self.store.get("fixer_usage", key) or {"id": key, "minutes": 0}
            usage["minutes"] += minutes
            self.store.put("fixer_usage", usage)

    def _payload(self, reasons):
        return {"ready": not reasons, "reasons": reasons,
                "checkout_root": str(self.checkout_root) if self.checkout_root else None, "repo": FIX_REPO,
                "minutes_today": self.minutes_today(), "daily_minutes": self._settings_minutes()}

    async def _has_token(self):
        try:
            return bool(await asyncio.to_thread(self.coordinator.credentials.get, TOKEN_REFERENCE))
        except keyring.errors.KeyringError:
            return False

    async def _git_origin_ok(self):
        code, text = await self._run(["git", "-C", str(self.checkout_root), "remote", "get-url", "origin"], cwd=self.checkout_root)
        return code == 0 and text.strip() in REPO_URLS

    async def _permission_ok(self):
        code, text = await self._run(["gh", "repo", "view", FIX_REPO, "--json", "viewerPermission"], cwd=self.checkout_root)
        if code != 0:
            return False
        try:
            return json.loads(text).get("viewerPermission") in WRITE_PERMISSIONS
        except (ValueError, AttributeError):
            return False

    async def status_payload(self, refresh=False):
        checked, cached = self._status
        if cached is not None and not refresh and time.monotonic() - checked < STATUS_CACHE_SECONDS:
            return cached
        if not sys.platform.startswith("linux"):
            payload = self._payload(["Fixes need Linux."])
        elif not self.checkout_root:
            payload = self._payload(["Fixes need ASHAD started from a git checkout (npm --prefix desktop start)."])
        else:
            root = Path(self.checkout_root)

            async def claude():
                code, _ = await self._run([str(Path.home() / ".local/bin/claude"), "--version"], cwd=root)
                return code == 0

            async def gh_auth():
                code, _ = await self._run(["gh", "auth", "status"], cwd=root)
                return code == 0

            async def local(value):
                return value

            checks = [  # D30 order; external=True checks stop running after the first failure
                (True, self._git_origin_ok, "The checkout's origin is not waspflannel/agentic-game-testing."),
                (False, lambda: local((root / ".venv/bin/python").exists()), "Run ./scripts/setup.sh in the checkout first."),
                (False, lambda: local(bool(self.which("jr", path=fixer_env().get("PATH")))), "Put jr on PATH."),
                (True, claude, "Install Claude Code at ~/.local/bin/claude."),
                (False, self._has_token, "Add your Claude subscription token in Settings."),
                (True, gh_auth, "Sign in with gh auth login."),
                (True, self._permission_ok, "Your GitHub account needs write access to waspflannel/agentic-game-testing."),
            ]
            reasons, external_failed = [], False
            for external, check, reason in checks:
                if external and external_failed:
                    continue
                if not await check():
                    reasons.append(reason)
                    external_failed = external_failed or external
            payload = self._payload(reasons)
        self._status = (time.monotonic(), payload)
        return payload

    def cached_status(self):
        cached = self._status[1]
        return cached or {"ready": False, "reasons": ["Checking the fixer…"], "checkout_root": None, "repo": FIX_REPO,
                          "minutes_today": 0, "daily_minutes": DEFAULT_DAILY_MINUTES}

    def create_loop(self, mode):
        return self.store.put("loops", {"id": uid("loop_"), "mode": mode, "phase": "playing", "created_at": time.time(),
                                        "current_run_id": None, "fixing_run_id": None, "issue": None, "attempts": [],
                                        "progress": [], "consecutive_failures": 0, "needs_you": None, "verify": None,
                                        "cancel_notes": []})

    def in_flight(self):
        for loop_id, task in self.tasks.items():
            loop = self.store.get("loops", loop_id)
            if not task.done() and loop and loop["phase"] in ACTIVE_PHASES:
                return [{"kind": "fixer"}]
        return []

    def restart_for_fix(self):
        if not self.checkout_root:
            return None
        return next((loop["id"] for loop in self.store.list("loops") if loop["phase"] == "restart_required"), None)

    # Stubs: Task 4 fills these in.
    def issue_hit(self, run_id, issue_type, issue_id, example_id, save_before):
        return None

    def run_finished(self, run):
        return None

    async def command(self, loop_id, command, command_id):
        return None

    def summary(self, loop):
        return loop

    async def cancel_processes(self):
        return None

    async def recover(self):
        return None
