"""Improvement loop fixer: fakes and temporary data only; no real gh, jr, claude, network or keyring."""
import asyncio
from collections import namedtuple
import contextlib
import io
import json
import os
import secrets
import shutil
import signal
import sys
import time
from types import SimpleNamespace

import keyring.errors
import pytest
from fastapi.testclient import TestClient

from ashad import fixer, repair_tasks
from ashad.claude_backend import TOKEN_REFERENCE
from ashad.coordinator import Coordinator
from ashad.fixer import Fixer
from ashad.repair_tasks import issue_directory, record_issue, set_issue_status
from ashad.store import Store
from ashad.app import create_app
from ashad.desktop import read_launch_config
from test_controller import FakeCodexBackend, MemoryCredentials, environment  # noqa: F401
from test_desktop_lifecycle import lifecycle
from test_instances import profile  # noqa: F401
from test_saves import PLAYER, act, events, no_http_inference, running  # noqa: F401
from test_unsupported_actions import basis, example

Call = namedtuple("Call", "argv cwd env timeout log_path")


def uid_hex():
    return secrets.token_hex(4)


class FakeShell:
    """Runner double. script: [(argv prefix, handler)]; handler is (rc, text) or async fn(call) -> (rc, text).

    `git -C <dir> ...` is matched on ("git", ...) with the -C dir dropped. The first matching entry wins."""

    def __init__(self, script=()):
        self.script = list(script)
        self.calls = []

    @staticmethod
    def key(argv):
        return ("git", *argv[3:]) if argv[:2] == ["git", "-C"] else tuple(argv)

    async def __call__(self, argv, *, cwd, env, timeout, log_path=None):
        call = Call(list(argv), cwd, env, timeout, log_path)
        self.calls.append(call)
        key = self.key(call.argv)
        for prefix, handler in self.script:
            if key[:len(prefix)] == tuple(prefix):
                code, text = await handler(call) if callable(handler) else handler
                if log_path:
                    with open(log_path, "a", encoding="utf-8") as stream:
                        stream.write(text)
                return code, text
        raise AssertionError(f"unscripted command: {call.argv}")


def fake_checkout(tmp_path, monkeypatch):
    """A checkout and a runtime directory with the same files; returns the checkout path."""
    checkout, runtime = tmp_path / "checkout", tmp_path / "runtime"
    for root in (checkout, runtime):
        (root / ".venv/bin").mkdir(parents=True)
        (root / ".venv/bin/python").write_text("python")
        (root / "ashad").mkdir()
        (root / "ashad/structured.py").write_text("structured")
    monkeypatch.setattr(fixer, "RUNTIME_ROOT", runtime)
    return checkout


def make_fixer(tmp_path, runner=None, credentials=None):
    store = Store(tmp_path / "data")
    coordinator = SimpleNamespace(store=store, credentials=credentials or MemoryCredentials(key=None))
    return Fixer(coordinator, runner)


def test_set_issue_status_appends_history_merges_fix_and_publishes(tmp_path):
    store = Store(tmp_path / "data")
    record_issue(store, "unsupported_action", basis(), example())
    issue_id = repair_tasks.issue_id("unsupported_action", basis())
    issue = set_issue_status(store, issue_id, "fixing", loop_id="loop_1", attempt_id=None, fix={"loop_id": "loop_1", "a": 1})
    assert issue["status"] == "fixing" and issue["fix"] == {"loop_id": "loop_1", "a": 1}
    [entry] = issue["status_history"]
    assert (entry["from"], entry["to"], entry["loop_id"], entry["attempt_id"]) == ("open", "fixing", "loop_1", None)
    set_issue_status(store, issue_id, "fixed", loop_id="loop_1", attempt_id="fix_1", fix={"a": 2})
    assert store.get("issues", issue_id)["fix"] == {"loop_id": "loop_1", "a": 2}
    [path] = issue_directory(store).glob("*.json")
    assert '"status": "fixed"' in path.read_text(encoding="utf-8")
    for _ in range(60):
        set_issue_status(store, issue_id, "open", loop_id="loop_1", attempt_id=None)
    assert len(store.get("issues", issue_id)["status_history"]) == 50
    assert set_issue_status(store, "issue_missing", "fixed", loop_id="l", attempt_id=None) is None


def test_record_issue_keeps_status_and_fix_on_later_hits(tmp_path):
    store = Store(tmp_path / "data")
    record_issue(store, "unsupported_action", basis(), example())
    issue_id = repair_tasks.issue_id("unsupported_action", basis())
    set_issue_status(store, issue_id, "fixing", loop_id="loop_1", attempt_id=None, fix={"loop_id": "loop_1"})
    record_issue(store, "unsupported_action", basis(), example(example_id="uae_2"))
    issue = store.get("issues", issue_id)
    assert issue["status"] == "fixing" and issue["fix"] == {"loop_id": "loop_1"} and issue["occurrences"] == 2


def test_default_runner_refuses_in_tests(tmp_path):
    with pytest.raises(AssertionError, match="real command through the fixer"):
        asyncio.run(make_fixer(tmp_path)._run(["gh", "auth", "status"], cwd=tmp_path))


def test_run_command_kills_process_group_on_timeout(tmp_path):
    pid_file = tmp_path / "pid"
    script = f"import os,time;open({str(pid_file)!r},'w').write(str(os.getpid()));os.fork() or None;time.sleep(30)"
    started = time.monotonic()
    code, _ = asyncio.run(fixer.run_command([sys.executable, "-c", script], cwd=str(tmp_path), env=fixer.fixer_env(), timeout=0.5))
    assert code is None and time.monotonic() - started < 15
    pgid = int(pid_file.read_text())
    for _ in range(50):  # the orphaned child is reaped asynchronously
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.1)
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


def test_run_command_returns_output_tail_to_pipe_and_log(tmp_path):
    argv = [sys.executable, "-c", "print('hello')"]
    assert asyncio.run(fixer.run_command(argv, cwd=str(tmp_path), env=fixer.fixer_env(), timeout=20)) == (0, "hello\n")
    log = tmp_path / "out.log"
    assert asyncio.run(fixer.run_command(argv, cwd=str(tmp_path), env=fixer.fixer_env(), timeout=20, log_path=log)) == (0, "hello\n")
    assert log.read_text() == "hello\n"


def test_fixer_env_is_allowlisted(monkeypatch):
    for name in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_API_KEY", "GH_TOKEN", "SECRET_X"):
        monkeypatch.setenv(name, "planted")
    assert set(fixer.fixer_env()) <= set(fixer.FIXER_ENV_KEYS)
    assert fixer.fixer_env("t")["CLAUDE_CODE_OAUTH_TOKEN"] == "t"
    assert fixer.jr_branch("fix_ab12") == "build/fix-ab12"


GOOD = [(("git", "remote", "get-url"), (0, fixer.REPO_URLS[0] + "\n")), (("gh", "auth", "status"), (0, "ok")),
        (("gh", "repo", "view"), (0, '{"viewerPermission": "WRITE"}'))]


def ready_fixer(tmp_path, monkeypatch, script=None):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    claude = (str(tmp_path / "home/.local/bin/claude"), "--version")
    shell = FakeShell([*(script if script is not None else GOOD), (claude, (0, "1.0"))])
    instance = make_fixer(tmp_path, shell, MemoryCredentials(key=None))
    instance.coordinator.credentials.save(TOKEN_REFERENCE, "fake-token")
    instance.which = lambda name, path=None: "/bin/jr"
    instance.checkout_root = fake_checkout(tmp_path, monkeypatch)
    return instance, shell


def status(instance):
    return asyncio.run(instance.status_payload(refresh=True))


def test_fixer_status_reasons(tmp_path, monkeypatch):
    instance, shell = ready_fixer(tmp_path, monkeypatch)
    assert status(instance) == {"ready": True, "reasons": [], "checkout_root": str(instance.checkout_root), "repo": fixer.FIX_REPO,
                                "minutes_today": 0, "daily_minutes": 240}
    count = len(shell.calls)
    assert asyncio.run(instance.status_payload()) is instance._status[1] and len(shell.calls) == count  # cached

    monkeypatch.setattr(sys, "platform", "win32")
    assert status(instance)["reasons"] == ["Fixes need Linux."]
    monkeypatch.setattr(sys, "platform", "linux")

    instance.checkout_root = None
    assert status(instance)["reasons"] == ["Fixes need ASHAD started from a git checkout (npm --prefix desktop start)."]
    instance.checkout_root = tmp_path / "checkout"

    shell.script.insert(0, (("git", "remote", "get-url"), (0, "https://github.com/someone/else\n")))
    assert status(instance)["reasons"][0] == "The checkout's origin is not waspflannel/agentic-game-testing."
    shell.script.pop(0)

    (tmp_path / "checkout/.venv/bin/python").unlink()
    assert status(instance)["reasons"] == ["Run ./scripts/setup.sh in the checkout first."]
    (tmp_path / "checkout/.venv/bin/python").write_text("python")

    instance.which = lambda *a, **k: None
    assert status(instance)["reasons"] == ["Put jr on PATH."]
    instance.which = lambda name, path=None: "/bin/jr"

    instance.coordinator.credentials.values.clear()
    assert status(instance)["reasons"] == ["Add your Claude subscription token in Settings."]
    instance.coordinator.credentials.get = lambda reference: (_ for _ in ()).throw(keyring.errors.KeyringError())
    assert status(instance)["reasons"] == ["Add your Claude subscription token in Settings."]
    instance.coordinator.credentials.get = lambda reference: "fake-token"

    shell.script.insert(0, (("gh", "auth", "status"), (1, "not logged in")))
    assert status(instance)["reasons"] == ["Sign in with gh auth login."]
    shell.script.pop(0)

    shell.script.insert(0, (("gh", "repo", "view"), (0, '{"viewerPermission": "READ"}')))
    assert status(instance)["reasons"] == ["Your GitHub account needs write access to waspflannel/agentic-game-testing."]
    shell.script.pop(0)

    claude_path = str(tmp_path / "home/.local/bin/claude")
    shell.script.insert(0, ((claude_path, "--version"), (1, "broken")))
    before = len(shell.calls)
    assert status(instance)["reasons"] == ["Install Claude Code at ~/.local/bin/claude."]
    assert not any(call.argv[:2] == ["gh", "auth"] for call in shell.calls[before:])  # no external checks after the first failure


def test_new_loop_and_safe_calls_on_missing_loops(tmp_path):
    instance = make_fixer(tmp_path)
    loop = instance.create_loop("auto")
    assert loop["phase"] == "playing" and instance.store.get("loops", loop["id"]) == loop
    assert instance.issue_hit("r", "unsupported_action", "i", "e", None) is None
    assert instance.run_finished({"id": "r"}) is None and instance.in_flight() == [] and instance.restart_for_fix() is None
    with pytest.raises(ValueError, match="Unknown loop"):
        asyncio.run(instance.command("loop_missing", "cancel", "c1"))
    assert asyncio.run(instance.recover()) is None
    instance.checkout_root = tmp_path
    instance.store.put("loops", {**loop, "phase": "restart_required"})
    assert instance.restart_for_fix() == loop["id"]
    instance._add_minutes(5)
    instance._add_minutes(7)
    assert instance.minutes_today() == 12


@pytest.mark.asyncio
async def test_intent_event_carries_target_kind(environment, monkeypatch):
    env = await running(environment, monkeypatch)
    await act(env)
    assert events(env, "structured_action_intent")[-1]["payload"]["target_kind"] == "chest"


@pytest.mark.asyncio
async def test_fixing_run_refuses_control_and_delete(environment, monkeypatch):
    env = await running(environment, monkeypatch)
    await env.coord.finish(env.rid, "issue_fix", final_state="Fixing")
    run = env.store.require("runs", env.rid)
    assert run["state"] == "Fixing"
    assert [(o["run_id"], o["reason"]) for o in env.coord.occupied_instances()] == [(env.rid, "fixing")]
    with pytest.raises(ValueError, match="This instance is fixing an issue. Use Cancel fix in the Improvement loop section."):
        await env.coord.control(env.rid, "stop", command_id="stop-fixing")
    with pytest.raises(ValueError):
        await env.coord.delete_run(env.rid)
    task = asyncio.create_task(env.coord.loop())
    await asyncio.sleep(0.1)
    env.coord.stopping = True
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert env.rid not in env.coord.ticks and env.store.require("runs", env.rid)["state"] == "Fixing"


@pytest.mark.asyncio
async def test_finish_default_still_ends(environment, monkeypatch):
    env = await running(environment, monkeypatch)
    await env.coord.finish(env.rid, "user_stopped")
    assert env.store.require("runs", env.rid)["state"] == "Ended"
    assert events(env, "run_ended")[-1]["payload"]["final_state"] == "Ended"


def test_launch_config_accepts_optional_checkout_root(tmp_path):
    config = {"data_root": str(tmp_path), "session_key": "s" * 43, "owner_key": "o" * 43, "instance_id": "launch-id"}

    def read(value):
        return read_launch_config(io.StringIO(json.dumps(value)))

    assert read(config) == config
    assert read({**config, "checkout_root": str(tmp_path)})["checkout_root"] == str(tmp_path)
    assert read({**config, "checkout_root": None})["checkout_root"] is None
    for changed in ({**config, "checkout_root": "relative"}, {**config, "checkout_root": str(tmp_path / "missing")},
                    {**config, "checkout_root": 5}, {**config, "checkout_root": str(tmp_path), "extra": 1}):
        with pytest.raises(ValueError, match="Invalid desktop launch configuration"):
            read(changed)


@pytest.mark.asyncio
async def test_desktop_status_hides_fixing_owner_and_flags_restart(environment, monkeypatch, tmp_path):
    env = await running(environment, monkeypatch)
    await env.coord.finish(env.rid, "issue_fix", final_state="Fixing")
    fixer_ = env.coord.fixer
    fixer_.checkout_root = tmp_path
    restart = fixer_.create_loop("auto")
    env.store.put("loops", {**restart, "phase": "restart_required"})
    busy = fixer_.create_loop("auto")
    env.store.put("loops", {**busy, "phase": "fixing"})
    env.store.put("loops", {**env.store.require("loops", busy["id"]), "attempts": [stub_attempt(1, "issue_x", result=None)]})
    sleeper = asyncio.create_task(asyncio.sleep(60))
    fixer_.tasks[busy["id"]] = sleeper
    quit, _, server = lifecycle(environment)
    try:
        status = quit.status()
        assert status["owners"] == [] and status["restart_for_fix"] == restart["id"]
        assert {"kind": "fixer"} in status["operations"]
        result = await quit.shutdown(stop_runs=False, cancel_operations=True, expected_run_ids=[])
        assert result["drained"] is True and server.should_exit
        assert env.store.require("runs", env.rid)["state"] == "Fixing"
        interrupted = env.store.require("loops", busy["id"])  # B3: quitting interrupts the running step; recover() decides what is next
        assert (interrupted["phase"], interrupted["attempts"][0]["result"], interrupted["consecutive_failures"]) == ("fixing", "interrupted", 1)
    finally:
        sleeper.cancel()
        await asyncio.gather(sleeper, return_exceptions=True)


def test_settings_fixer_daily_minutes(tmp_path):
    app = create_app(tmp_path / "data", credentials=MemoryCredentials(), start_services=False, ai_backend=FakeCodexBackend())
    headers = {"X-ASHAD-Request": "1"}
    with TestClient(app) as client:
        assert client.get("/api/bootstrap").json()["settings"]["fixer_daily_minutes"] == 240
        for bad in (0, 1441, True, "5"):
            assert client.post("/api/settings", json={"fixer_daily_minutes": bad}, headers=headers).status_code == 422
        assert app.state.store.require("settings", "main")["fixer_daily_minutes"] == 240
        assert client.post("/api/settings", json={"fixer_daily_minutes": 30}, headers=headers).status_code == 200
        assert app.state.store.require("settings", "main")["fixer_daily_minutes"] == 30


# ---- state machine harness: a scripted gh/git/jr world around a real Coordinator and store -----------------------------

PR_URL = "https://github.com/waspflannel/agentic-game-testing/pull/{}"
PLANTED = {"ANTHROPIC_API_KEY": "sk-ant-PLANTED", "OPENAI_API_KEY": "sk-openai-PLANTED", "CODEX_API_KEY": "codex-PLANTED",
           "GH_TOKEN": "ghp_PLANTED"}


class Cycle:
    """Scripted answers for one fix PR (#7) and its revert PR (#8). Every handler reads the fields when called, so a test may
    change `diff`, `tests`, `jr`, `parents`, ... at any time. `tests` lists the exit codes of successive scripts/test.sh runs
    (gate, post-merge, revert gate, revert post-merge); the last one repeats."""

    def __init__(self):
        self.pr, self.head, self.merge, self.base = 7, "a" * 40, "b" * 40, "c" * 40
        self.revert_pr, self.revert_head, self.revert_merge = 8, "d" * 40, "e" * 40
        self.diff = "M\tashad/game_mcp.py\nA\ttests/test_new_verb.py\n"
        self.tests = [0]
        self.jr = [(0, "SHIP: done")]  # successive answers of jr ship; the last repeats
        self.jr_gate = None  # an asyncio.Event jr waits for
        self.build_log = None
        self.pr_exists, self.base_ref, self.cross_repo = True, "main", False
        self.parents = {}  # merge sha -> `git rev-list --parents` line, default: merge commit of the gated head
        self.porcelain, self.branch, self.pull_rc = "", "main", 0
        self.merged = set()  # PR numbers merged on "GitHub"
        self.test_runs = 0
        self.ls_remote_rc = 2  # `git ls-remote --exit-code`: 0 when the revert branch is on the remote, 2 when it is not

    def user_merges(self, number):
        self.merged.add(number)

    def script(self):
        return [
            (("git", "merge-base", "--is-ancestor"), (0, "")), (("git", "merge-base"), self.on_merge_base),
            (("git", "remote", "get-url"), (0, fixer.REPO_URLS[0] + "\n")), (("git", "diff"), self.on_diff),
            (("git", "rev-list"), self.on_parents), (("git", "status"), self.on_status),
            (("git", "rev-parse"), self.on_branch), (("git", "pull"), self.on_pull), (("git", "worktree", "add"), self.on_worktree),
            (("git", "worktree", "remove"), self.on_worktree_remove), (("git", "ls-remote"), self.on_ls_remote), (("git", "fetch"), (0, "")), (("git", "checkout"), (0, "")),
            (("git", "revert"), (0, "")), (("git", "push"), (0, "")),
            (("jr", "ship"), self.on_jr), (("scripts/test.sh",), self.on_tests),
            (("gh", "pr", "list"), self.on_list), (("gh", "pr", "view"), self.on_view), (("gh", "pr", "merge"), self.on_merge),
            (("gh", "pr", "create"), (0, PR_URL.format(self.revert_pr))), (("gh", "pr", "close"), (0, "")),
            (("gh", "pr", "comment"), (0, ""))]

    async def on_merge_base(self, call):
        return 0, self.base + "\n"

    async def on_diff(self, call):
        return 0, self.diff

    async def on_parents(self, call):
        sha = call.argv[-1]
        head = self.revert_head if sha == self.revert_merge else self.head
        return 0, self.parents.get(sha, f"{sha} {self.base} {head}") + "\n"

    async def on_status(self, call):
        return 0, self.porcelain

    async def on_branch(self, call):
        return 0, self.branch + "\n"

    async def on_pull(self, call):
        return self.pull_rc, ""

    async def on_worktree(self, call):
        if os.path.exists(call.argv[-2]):
            return 128, "already exists"  # like git
        os.makedirs(call.argv[-2])
        return 0, ""

    async def on_worktree_remove(self, call):
        shutil.rmtree(call.argv[-1], ignore_errors=True)
        return 0, ""

    async def on_ls_remote(self, call):
        return self.ls_remote_rc, ""

    async def on_jr(self, call):
        if self.jr_gate:
            await self.jr_gate.wait()
        if self.build_log is not None:
            log = fixer.Path(call.cwd) / ".notes/builds" / (fixer.Path(call.argv[-1]).stem + ".log")
            log.parent.mkdir(parents=True, exist_ok=True)
            log.write_text(self.build_log, encoding="utf-8")
        return self.jr.pop(0) if len(self.jr) > 1 else self.jr[0]

    async def on_tests(self, call):
        self.test_runs += 1
        return self.tests[min(self.test_runs, len(self.tests)) - 1], "tests output\n"

    def listing(self, head, number, sha):
        return json.dumps([{"number": number, "url": PR_URL.format(number), "state": "OPEN", "headRefOid": sha,
                            "baseRefName": self.base_ref, "isCrossRepository": self.cross_repo}])

    async def on_list(self, call):
        head = call.argv[call.argv.index("--head") + 1]
        if head.startswith("revert/"):
            return 0, self.listing(head, self.revert_pr, self.revert_head)
        return 0, self.listing(head, self.pr, self.head) if self.pr_exists else "[]"

    async def on_view(self, call):
        number = int(call.argv[3])
        sha = self.merge if number == self.pr else self.revert_merge
        if number in self.merged:
            return 0, json.dumps({"state": "MERGED", "mergeCommit": {"oid": sha}, "headRefOid": self.head})
        return 0, json.dumps({"state": "OPEN", "mergeCommit": None, "headRefOid": self.head})

    async def on_merge(self, call):
        self.merged.add(int(call.argv[3]))
        return 0, ""


class World:
    """A running instance with a fix loop, the scripted shell and (after restart) a second controller over the same store."""

    def __init__(self, env, cycle, shell, loop_id, checkout):
        self.env, self.cycle, self.shell, self.loop_id, self.checkout = env, cycle, shell, loop_id, checkout
        self.coord = env.coord
        self.store = env.store
        self.verify_run_id = None

    @property
    def fx(self):
        return self.coord.fixer

    def loop(self):
        return self.store.get("loops", self.loop_id)

    def issue(self):
        [issue] = [i for i in self.store.list("issues") if i["id"] == self.loop()["issue"]["issue_id"]]
        return issue

    def calls(self, *prefix):
        """Argv of the shell calls starting with `prefix` (git -C <dir> is matched without the dir)."""
        return [c.argv for c in self.shell.calls if FakeShell.key(c.argv)[:len(prefix)] == prefix]

    async def settle(self, timeout=20):
        """Wait until the loop's driver task is done, or only the verification watcher is left."""
        async def wait():
            while (task := self.fx.tasks.get(self.loop_id)) and not task.done() and self.loop()["phase"] != "verifying":
                await asyncio.wait({task}, timeout=0.05)
        await asyncio.wait_for(wait(), timeout)

    async def until(self, predicate, timeout=20):
        async def wait():
            while not predicate(self.loop()):
                await asyncio.sleep(0.02)
        await asyncio.wait_for(wait(), timeout)

    async def hit(self):
        """The agent asks for an action the worker rejects: a real unsupported_action hit on the first run."""
        await act(self.env)
        await self.settle()

    def direct_hit(self, run_id=None, save="save-x", basis_=None, **example_fields):
        """A hit recorded straight through record_issue and issue_hit (for runs we do not drive)."""
        basis_ = basis_ or basis()
        example_id = "uae_" + uid_hex()
        record_issue(self.store, "unsupported_action", basis_, example(example_id=example_id, **example_fields))
        issue_id = repair_tasks.issue_id("unsupported_action", basis_)
        self.fx.issue_hit(run_id or self.env.rid, "unsupported_action", issue_id, example_id, save)
        return issue_id

    def restart(self):
        """A new controller over the same store and checkout, as after an Electron relaunch."""
        old = self.coord
        self.coord = Coordinator(self.store, old.credentials, old.ai, old.client_factory)
        self.coord.fixer.runner = self.shell
        self.coord.fixer.checkout_root = self.checkout
        self.env.coord = self.coord
        return self.coord.fixer

    async def apply_and_restart(self):
        """After `restart_required`: relaunch the controller, recover, and wait for the loop to move on."""
        assert self.loop()["phase"] == "restart_required"
        self.restart()
        await self.coord.recover()
        await self.settle()

    async def verifying(self):
        """Hit -> full Auto cycle -> restart -> a check run exists and is watched. Returns the check run id."""
        await self.hit()
        assert self.loop()["phase"] == "restart_required", self.loop()
        await self.apply_and_restart()
        assert self.loop()["phase"] == "verifying", self.loop()
        self.verify_run_id = self.loop()["verify"]["run_id"]
        self.set_run(state="Running")
        return self.verify_run_id

    def set_run(self, run_id=None, **fields):
        run = self.store.require("runs", run_id or self.verify_run_id)
        run.update(fields)
        self.store.put("runs", run)

    def verify_events(self, *specs):
        for kind, payload in specs:
            self.store.event(self.verify_run_id, kind, payload)


@contextlib.asynccontextmanager
async def fix_world(environment, monkeypatch, tmp_path, profile, mode="auto"):
    env = await running(environment, monkeypatch, profile, reject_verbs={"interact"})
    for name, value in PLANTED.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(fixer, "WATCH_INTERVAL_SECONDS", 0.05)
    checkout = fake_checkout(tmp_path, monkeypatch)
    cycle = Cycle()
    shell = FakeShell(cycle.script())
    fx = env.coord.fixer
    fx.runner, fx.checkout_root = shell, checkout
    loop = fx.create_loop(mode)
    run = env.store.require("runs", env.rid)
    run["loop_id"] = loop["id"]
    env.store.put("runs", run)
    world = World(env, cycle, shell, loop["id"], checkout)
    try:
        yield world
    finally:
        for coordinator in {env.coord, world.coord}:
            for task in coordinator.fixer.tasks.values():
                task.cancel()
            await asyncio.gather(*coordinator.fixer.tasks.values(), return_exceptions=True)


# ---- state machine tests --------------------------------------------------------------------------------------------

USAGE = (0, "Claude usage limit reached")
DONE = (0, "SHIP: done")
SECRETS = (*PLANTED.values(), "oat-PLANTED")


@pytest.mark.asyncio
async def test_off_instance_never_starts_fix(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        run = world.store.require("runs", world.env.rid)
        run.pop("loop_id")
        world.store.put("runs", run)
        before = world.loop()
        await act(world.env)
        await asyncio.sleep(0.2)
        assert world.loop() == before and world.shell.calls == [] and world.fx.tasks == {}
        assert world.store.require("runs", world.env.rid)["state"] == "Running"
        assert world.store.list("issues")[0]["status"] == "open"
        assert not events(world.env, "run_ended")


@pytest.mark.asyncio
async def test_manual_hit_moves_run_to_fixing_and_waits(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile, mode="manual") as world:
        await world.hit()
        run, loop, issue = world.store.require("runs", world.env.rid), world.loop(), world.issue()
        assert run["state"] == "Fixing"
        assert all(ps["teardown_confirmed"] is True for ps in run["players"].values())
        assert [(o["run_id"], o["reason"]) for o in world.coord.occupied_instances()] == [(world.env.rid, "fixing")]
        assert (loop["phase"], loop["needs_you"]["reason"], loop["needs_you"]["offers"]) == ("needs_you", "dispatch", ["dispatch", "cancel"])
        assert issue["status"] == "fixing" and [h["to"] for h in issue["status_history"]] == ["fixing"]
        assert world.calls("jr") == [] and world.shell.calls == []
        [row] = loop["progress"]
        assert row["run_id"] == world.env.rid and row["stopped_by"] == issue["title"] and row["further"] is None
        assert loop["issue"]["save_id"] and loop["current_run_id"] == loop["fixing_run_id"] == world.env.rid
        assert events(world.env, "fix_needs_you")[-1]["payload"]["reason"] == "dispatch"


@pytest.mark.asyncio
async def test_auto_full_cycle_merges_applies_and_verifies(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        world.coord.credentials.save(TOKEN_REFERENCE, "oat-PLANTED")
        world.cycle.diff = "M\tashad/structured.py\nA\ttests/test_new_verb.py\n"
        await world.hit()
        # (1) the hit stops the run and moves it to Fixing
        assert world.store.require("runs", world.env.rid)["state"] == "Fixing"
        loop = world.loop()
        [attempt] = loop["attempts"]
        assert loop["phase"] == "restart_required" and attempt["merge_sha"] == world.cycle.merge
        # (2) jr "opened" PR #7 on build/fix-<hex>
        assert attempt["pr"]["number"] == 7 and attempt["branch"] == fixer.jr_branch(attempt["id"])
        assert attempt["branch"].startswith("build/fix-")
        [jr] = world.calls("jr", "ship")
        assert jr[-1].endswith(f"{attempt['id']}.md")
        # (3) the gate commands run in the order of D17; (4) the merge is called exactly once with the gated sha
        merges = world.calls("gh", "pr", "merge")
        assert merges == [["gh", "pr", "merge", "7", "--repo", fixer.FIX_REPO, "--merge", "--match-head-commit", world.cycle.head]]
        wt, co = attempt["worktree"], str(world.checkout)
        sha, base = world.cycle.head, world.cycle.base
        assert [c.argv for c in world.shell.calls] == [
            ["git", "-C", co, "fetch", "origin", "main"],
            ["git", "-C", co, "worktree", "add", "--detach", wt, "origin/main"],
            jr,
            ["gh", "pr", "list", "--repo", fixer.FIX_REPO, "--head", attempt["branch"], "--state", "all", "--json", fixer.PR_FIELDS],
            ["git", "-C", wt, "fetch", "origin", "main"],
            ["git", "-C", wt, "fetch", "origin", "pull/7/head"],
            ["git", "-C", wt, "checkout", "--detach", sha],
            ["git", "-C", wt, "merge-base", "origin/main", "HEAD"],
            ["git", "-C", wt, "diff", "--no-renames", "--name-status", base, "HEAD"],
            ["scripts/test.sh"],
            merges[0],
            ["gh", "pr", "view", "7", "--repo", fixer.FIX_REPO, "--json", "state,mergeCommit,headRefOid"],
            ["git", "-C", wt, "fetch", "origin", "main"],
            ["git", "-C", wt, "rev-list", "--parents", "-n", "1", world.cycle.merge],
            ["git", "-C", wt, "checkout", "--detach", world.cycle.merge],
            ["scripts/test.sh"],
            ["git", "-C", co, "status", "--porcelain"],
            ["git", "-C", co, "rev-parse", "--abbrev-ref", "HEAD"],
            ["git", "-C", co, "pull", "--ff-only", "origin", "main"],
            ["git", "-C", co, "merge-base", "--is-ancestor", world.cycle.merge, "HEAD"],
            ["git", "-C", co, "worktree", "remove", "--force", wt]]
        gate = [c for c in world.shell.calls if c.argv == ["scripts/test.sh"]]
        assert [c.cwd for c in gate] == [wt, wt] and gate[0].timeout == fixer.GATE_TIMEOUT_SECONDS
        assert gate[0].log_path.name == "gate.log"
        assert world.calls("jr")[0] and next(c for c in world.shell.calls if c.argv[0] == "jr").env["CLAUDE_CODE_OAUTH_TOKEN"] == "oat-PLANTED"
        assert world.fx.restart_for_fix() == world.loop_id
        # (5) a new Coordinator with runtime bytes equal to the checkout recovers and loads the save with the same loop_id
        world.restart()
        await world.coord.recover()
        await world.settle()
        loop = world.loop()
        assert loop["phase"] == "verifying"
        check = world.store.require("runs", loop["verify"]["run_id"])
        assert check["loop_id"] == world.loop_id and check["rerun_kind"] == "save_load" and check["id"] != world.env.rid
        assert world.store.require("runs", world.env.rid)["state"] == "Ended"
        assert world.store.require("runs", world.env.rid)["ending_reason"] == "fix_applied"
        # (6) a completed action with the wanted verb and target kind fixes the issue and adds a progress row
        world.verify_run_id = check["id"]
        world.set_run(state="Running")
        world.verify_events(
            ("structured_action_intent", {"player_id": PLAYER, "action_id": "act-1", "target_kind": "chest",
                                          "input": {"verb": "interact", "target_id": "chest-1"}}),
            ("action_result", {"player_id": PLAYER, "action_id": "act-1", "result": {"status": "completed"}}))
        await world.until(lambda l: l["phase"] == "playing")
        issue = world.issue()
        assert issue["status"] == "fixed" and issue["fix"]["verified"]["pass"] == "action_worked"
        assert issue["fix"]["attempt_id"] == attempt["id"] and issue["fix"]["merge_sha"] == world.cycle.merge
        loop = world.loop()
        assert loop["attempts"][0]["result"] == "verified" and loop["consecutive_failures"] == 0
        assert loop["current_run_id"] == check["id"]
        assert [e["payload"]["passed"] for e in events(world.env, "fix_verified", check["id"])] == ["action_worked"]
        await world.coord.finish(check["id"], "user_stopped")
        [first, second] = world.loop()["progress"]
        assert first["run_id"] == world.env.rid and second["run_id"] == check["id"] and second["fix_before"] == attempt["id"]
        assert second["further"] in ("more", "same", "less")


def seeded_crash_sections():
    return {"crash": {"kind": "game_exit", "detector": "worker_status", "reason": "gone", "exit_code": 1, "signal": None,
                      "log_tail": "tail", "worker_status": {}}, "last_action": None, "timing": {}}


@pytest.mark.asyncio
async def test_crash_and_game_bug_are_never_fixed(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        run, ps = world.coord.structured.binding(world.env.rid, PLAYER)
        crash = {"kind": "game_exit", "detector": "worker_status", "exit": "signal:SIGSEGV"}
        await world.coord.structured._record(run, ps, PLAYER, "crash", crash, "monitor", world.env.observation, seeded_crash_sections())
        bug = {"symptom": "wrong_result", "expected": "state_change", "observed": "no_change", "action_verb": "interact",
               "target_kind": "chest", "target_label": "chest #"}
        await world.coord.structured._record(run, ps, PLAYER, "game_bug", bug, "agent_signal", world.env.observation,
                                             {"report": {"symptom": "wrong_result"}})
        await asyncio.sleep(0.2)
        assert {i["type"] for i in world.store.list("issues")} == {"crash", "game_bug"}
        assert world.store.require("runs", world.env.rid)["state"] == "Running"
        assert world.shell.calls == [] and world.fx.tasks == {}
        assert world.loop()["phase"] == "playing" and world.loop()["issue"] is None


@pytest.mark.asyncio
async def test_no_save_needs_you(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        world.direct_hit(save=None)
        await world.settle()
        loop = world.loop()
        assert loop["phase"] == "needs_you" and loop["needs_you"]["reason"] == "no_save" and loop["needs_you"]["offers"] == ["cancel"]
        assert world.calls("jr") == [] and world.shell.calls == []
        assert world.store.require("runs", world.env.rid)["state"] == "Fixing"


def all_text(world):
    """Everything a secret could leak into: files under the fixer directory, ledger events of every run, records, issue files."""
    parts = [path.read_text(encoding="utf-8", errors="replace") for path in (world.store.root / "fixer").rglob("*")
             if path.is_file() and not path.is_symlink()]
    parts += [json.dumps(world.store.events(run["id"], limit=5000), default=str) for run in world.store.list("runs")]
    parts += [json.dumps(world.store.list("loops"), default=str), json.dumps(world.store.list("issues"), default=str)]
    parts += [path.read_text(encoding="utf-8") for path in issue_directory(world.store).glob("*.json")]
    return "\n".join(parts)


def flag_value(argv, flag):
    return argv[argv.index(flag) + 1] if flag in argv else None


async def fix_then_revert(world):
    """Auto: fix merged and applied, the issue recurs while verifying, the fix is reverted and applied, attempt 2 hits the usage limit."""
    world.cycle.jr = [DONE, USAGE]
    vid = await world.verifying()
    world.direct_hit(run_id=vid, basis_=world.issue()["signature_basis"])
    await world.until(lambda l: l["phase"] == "restart_required")
    await world.apply_and_restart()
    assert world.loop()["needs_you"]["reason"] == "usage_limit"
    return vid


@pytest.mark.asyncio
async def test_planted_secrets_never_leak(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        world.coord.credentials.save(TOKEN_REFERENCE, "oat-PLANTED")
        await fix_then_revert(world)
        assert len(world.calls("gh", "pr", "create")) == 1 and len(world.calls("git", "revert")) == 1
        assert os.environ["GH_TOKEN"] == "ghp_PLANTED"
        for call in world.shell.calls:
            text = json.dumps(call.argv)
            assert not any(secret in text for secret in SECRETS), call.argv
            assert set(call.env) <= set(fixer.FIXER_ENV_KEYS) | {"CLAUDE_CODE_OAUTH_TOKEN"}
            assert ("CLAUDE_CODE_OAUTH_TOKEN" in call.env) == (call.argv[:2] == ["jr", "ship"]), call.argv
            if call.log_path:
                assert not any(secret in fixer.Path(call.log_path).read_text(encoding="utf-8") for secret in SECRETS)
        jr_envs = [call.env for call in world.shell.calls if call.argv[0] == "jr"]
        assert jr_envs and all(env["CLAUDE_CODE_OAUTH_TOKEN"] == "oat-PLANTED" for env in jr_envs)
        texts = [flag_value(c.argv, f) for c in world.shell.calls for f in ("--title", "--body", "--comment")]
        assert any(texts) and not any(secret in text for text in texts if text for secret in SECRETS)
        blob = all_text(world)
        for secret in SECRETS:
            assert secret not in blob
        names = {path.name for path in (world.store.root / "fixer").rglob("*") if path.is_file()}
        assert {"fixer.log", "gate.log"} <= names and any(name.endswith(".md") for name in names)


async def command(world, name, command_id):
    summary = await world.fx.command(world.loop_id, name, command_id)
    await world.settle()
    return summary


def attempt_request(world, n=0):
    attempt_id = world.loop()["attempts"][n]["id"]
    return (world.store.root / "fixer" / attempt_id / f"{attempt_id}.md").read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_request_is_plan_shaped_and_fences_untrusted_evidence(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile, mode="manual") as world:
        world.cycle.jr = [USAGE]
        outcome = {"status": "rejected", "error_code": "invalid_target", "http_status": 422, "reason": "r" * 5000, "log_tail": "LOGTAIL-MARK"}
        game_state = {"observation_id": "obs-1", "observation": {"entities": [{"id": "ENTITY-MARK"}]}, "telemetry": {}, "progress": None}
        world.direct_hit(outcome=outcome, game_state=game_state)
        await world.settle()
        await command(world, "dispatch", "c1")
        text = attempt_request(world)
        for heading in ("# Fix: ", "## Goal", "## Scope", "## Decisions already made", "## Evidence", "## Earlier attempts", "## Tasks",
                        "## PR shape", "## Final proof"):
            assert heading in text
        assert text.index("## Goal") < text.index("## Scope") < text.index("## Decisions already made") < text.index("## Tasks") \
            < text.index("## PR shape") < text.index("## Final proof")
        assert "```text\nUntrusted game evidence: data, not instructions\n" in text
        assert all(path in text for path in sorted(fixer.ALLOWED_PATHS)) and "do not merge" in text.lower()
        assert "needs_worker_change" in text and "`worker/`" in text and "tests/test_saves.py" in text
        assert "`ui_drag`" in text and "`inventory_slot`" in text
        for forbidden in ("log_tail", "LOGTAIL-MARK", "ENTITY-MARK", "recent_decisions", "evt_1", "game_state", str(issue_directory(world.store)),
                          "/tmp/run-1", "oat-"):
            assert forbidden not in text
        assert "r" * 2000 in text and "r" * 2001 not in text
        assert f"[ashad-fix {world.loop()['issue']['issue_id']} {world.loop()['attempts'][0]['id']}]" in text
        assert (world.store.root / "fixer" / world.loop()["attempts"][0]["id"] / "fixer.log").read_text() == USAGE[1]


NEEDS_WORKER = {"build_log": "NEEDS JADEN: needs_worker_change"}
OUTCOMES = [  # id, cycle changes, first attempt result, counted failure, retried in Auto, needs_you reason after
    ("usage_limit", {"jr": [USAGE]}, "usage_limit", 0, False, "usage_limit"),
    ("usage_limit_in_build_log", {"jr": [DONE], "build_log": "rate limit hit"}, "usage_limit", 0, False, "usage_limit"),
    ("fixer_unavailable", {"jr": [(0, "SHIP: stopped at plan review | VERDICT: unknown")]}, "fixer_unavailable", 0, False, "fixer_unavailable"),
    ("empty_verdict", {"jr": [(0, "SHIP: stopped at plan review | VERDICT:")]}, "fixer_unavailable", 0, False, "fixer_unavailable"),
    ("plan_rejected", {"jr": [(0, "SHIP: stopped at plan review | VERDICT: revise"), USAGE]}, "plan_rejected", 1, True, "usage_limit"),
    ("needs_worker_change", {"jr": [DONE], **NEEDS_WORKER}, "needs_worker_change", 0, False, "needs_worker_change"),
    ("timeout", {"jr": [(None, ""), USAGE]}, "timeout", 1, True, "usage_limit"),
    ("no_pr", {"jr": [DONE, USAGE], "pr_exists": False}, "no_pr", 1, True, "usage_limit"),
    ("wrong_base", {"jr": [DONE, USAGE], "base_ref": "dev"}, "wrong_base", 1, True, "usage_limit"),
    ("cross_repo", {"jr": [DONE, USAGE], "cross_repo": True}, "wrong_base", 1, True, "usage_limit")]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", OUTCOMES, ids=[case[0] for case in OUTCOMES])
async def test_jr_outcomes(case, environment, monkeypatch, tmp_path, profile):
    _, changes, result, counted, retried, reason = case
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        for name, value in changes.items():
            setattr(world.cycle, name, value)
        await world.hit()
        loop = world.loop()
        assert loop["attempts"][0]["result"] == result
        assert len(loop["attempts"]) == (2 if retried else 1)
        assert loop["consecutive_failures"] == counted
        assert loop["phase"] == "needs_you" and loop["needs_you"]["reason"] == reason
        assert world.calls("gh", "pr", "merge") == [] and world.calls("scripts/test.sh") == []
        if reason == "usage_limit" and not retried:
            assert world.calls("gh", "pr", "list") == [] and world.issue()["status"] == "fixing"  # dispatch may retry
        if result == "needs_worker_change":
            assert world.issue()["status"] == "open" and world.calls("gh", "pr", "list") == []
        if result == "wrong_base":
            assert len(world.calls("gh", "pr", "close")) == 1  # an open PR on the wrong base is closed, not left behind


DISALLOWED = ["M\tworker/runtime.py\n", "M\ttests/test_saves.py\n", "D\ttests/test_ai.py\n", "M\tashad/fixer.py\n",
              "M\tashad/static/app.js\n", "M\tdesktop/main.cjs\n"]


@pytest.mark.asyncio
@pytest.mark.parametrize("diff", DISALLOWED)
async def test_disallowed_paths_close_pr_and_count_failure(diff, environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        world.cycle.diff = "M\tashad/game_mcp.py\n" + diff
        world.cycle.jr = [DONE, USAGE]
        await world.hit()
        loop = world.loop()
        assert loop["attempts"][0]["result"] == "disallowed_paths" and loop["consecutive_failures"] == 1
        assert loop["attempts"][0]["changed_paths"][-1] == diff.split("\t")[1].strip()
        [close] = world.calls("gh", "pr", "close")
        assert close[:5] == ["gh", "pr", "close", "7", "--repo"] and "disallowed_paths" in flag_value(close, "--comment")
        assert world.calls("scripts/test.sh") == [] and world.calls("gh", "pr", "merge") == []
        assert world.calls("git", "worktree", "remove")  # the failed attempt's worktree is cleaned up
        assert len(loop["attempts"]) == 2 and loop["needs_you"]["reason"] == "usage_limit"


@pytest.mark.asyncio
async def test_tests_failed_blocks_merge(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        world.cycle.tests = [1]
        world.cycle.jr = [DONE, USAGE]
        await world.hit()
        loop = world.loop()
        assert loop["attempts"][0]["result"] == "tests_failed" and loop["consecutive_failures"] == 1
        assert world.calls("gh", "pr", "merge") == [] and len(world.calls("scripts/test.sh")) == 1
        assert len(world.calls("gh", "pr", "close")) == 1
        assert len(loop["attempts"]) == 2 and loop["needs_you"]["reason"] == "usage_limit"
        assert [e["payload"]["result"] for e in events(world.env, "fix_gate_failed")] == ["tests_failed"]


BANNED = ("--admin", "--squash", "--rebase", "--force", "-f")


@pytest.mark.asyncio
async def test_guardrails_on_every_command(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        await fix_then_revert(world)
        worktrees, cycle = world.store.root / "fixer" / "worktrees", world.cycle
        heads = {str(cycle.pr): cycle.head, str(cycle.revert_pr): cycle.revert_head}
        for call in world.shell.calls:
            argv = call.argv
            if argv[0] == "gh":
                assert flag_value(argv, "--repo") == "waspflannel/agentic-game-testing", argv
            if argv[:3] == ["gh", "pr", "merge"]:
                assert "--merge" in argv and flag_value(argv, "--match-head-commit") == heads[argv[3]], argv
            if argv[:2] == ["git", "-C"] and argv[3:6] == ["worktree", "remove", "--force"]:  # the only allowed --force: local cleanup
                assert len(argv) == 7 and fixer.Path(argv[6]).parent == worktrees, argv
                continue
            assert not any(arg in BANNED or "+HEAD" in arg for arg in argv), argv
            if argv[3:4] == ["push"]:
                assert argv[4] == "origin" and argv[5].startswith("HEAD:refs/heads/revert/ashad-") and len(argv) == 6, argv
        pushes = world.calls("git", "push")
        assert len(pushes) == 1 and len(world.calls("gh", "pr", "merge")) == 2
        assert [argv[3] for argv in world.calls("gh", "pr", "merge")] == ["7", "8"]
        assert any(argv[3:6] == ["worktree", "remove", "--force"] for argv in (c.argv for c in world.shell.calls))


@pytest.mark.asyncio
async def test_manual_mode_never_merges(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile, mode="manual") as world:
        await world.hit()
        assert world.loop()["needs_you"]["reason"] == "dispatch"
        await command(world, "dispatch", "c-dispatch")
        loop = world.loop()
        assert loop["needs_you"] == {"reason": "review_pr", "offers": ["apply", "cancel"], "message": fixer.NEEDS_YOU["review_pr"]}
        assert loop["attempts"][0]["pr"]["number"] == 7 and world.calls("scripts/test.sh") != []
        await command(world, "apply", "c-apply-early")  # not merged yet: still waiting
        assert world.loop()["needs_you"]["reason"] == "review_pr" and world.loop()["needs_you"]["message"] == fixer.NEEDS_YOU["not_merged"]
        world.cycle.user_merges(7)
        await command(world, "apply", "c-apply")
        assert world.loop()["phase"] == "restart_required" and world.loop()["attempts"][0]["merge_sha"] == world.cycle.merge
        await world.apply_and_restart()
        vid = world.loop()["verify"]["run_id"]
        world.set_run(vid, state="Running")
        world.verify_run_id = vid
        world.direct_hit(run_id=vid, basis_=world.issue()["signature_basis"])
        await world.settle()
        loop = world.loop()
        assert loop["needs_you"]["reason"] == "verify_failed" and loop["needs_you"]["offers"] == ["revert", "dispatch", "cancel"]
        await command(world, "revert", "c-revert")
        assert world.loop()["needs_you"]["reason"] == "review_revert"
        assert world.loop()["attempts"][0]["revert"]["pr"]["number"] == 8
        world.cycle.user_merges(8)
        await command(world, "apply", "c-apply-revert")
        assert world.loop()["phase"] == "restart_required"
        await world.apply_and_restart()
        loop = world.loop()
        assert loop["needs_you"]["reason"] == "reverted" and loop["needs_you"]["offers"] == ["dispatch", "cancel"]
        assert world.issue()["status"] == "open" and world.issue()["fix"]["reverted"]["sha"] == world.cycle.revert_merge
        assert world.calls("gh", "pr", "merge") == []
        assert len(world.calls("gh", "pr", "view")) >= 3


@pytest.mark.asyncio
@pytest.mark.parametrize("parents", ["{merge} {base}", "{merge} {base} " + "f" * 40, "{merge} {head} {base}"],
                         ids=["one_parent", "wrong_parent_2", "head_is_parent_1"])
async def test_merge_must_be_the_gated_head(parents, environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        cycle = world.cycle
        cycle.parents[cycle.merge] = parents.format(merge=cycle.merge, base=cycle.base, head=cycle.head)
        await world.hit()
        loop = world.loop()
        assert loop["phase"] == "needs_you" and loop["needs_you"]["reason"] == "merged_untested"
        assert loop["needs_you"]["offers"] == ["revert", "cancel"] and loop["attempts"][0]["merge_sha"] == cycle.merge
        assert world.calls("git", "pull") == [] and len(world.calls("scripts/test.sh")) == 1
        assert len(world.calls("gh", "pr", "merge")) == 1 and world.fx.restart_for_fix() is None


def stub_attempt(n, issue_id, **fields):
    return {"id": f"fix_stub{n:08d}", "n": n, "issue_id": issue_id, "override": False, "started_at": 0, "ended_at": 1, "minutes": 0,
            "branch": "b", "worktree": "/nonexistent", "pr": None, "changed_paths": [], "merge_sha": None, "result": "usage_limit",
            "revert": None, **fields}


def seed_attempts(world, count, **fields):
    loop = world.loop()
    loop["attempts"] = [stub_attempt(n, loop["issue"]["issue_id"], **fields) for n in range(1, count + 1)]
    world.store.put("loops", loop)


@pytest.mark.asyncio
async def test_caps_and_override(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        world.cycle.jr = [USAGE]  # not a counted failure, so only the per-issue cap can stop the loop
        await world.hit()
        for n in (2, 3):
            assert world.loop()["needs_you"]["reason"] == "usage_limit" and len(world.loop()["attempts"]) == n - 1
            await command(world, "dispatch", f"c{n}")
        await command(world, "dispatch", "c4")
        loop = world.loop()
        assert len(loop["attempts"]) == 3 and loop["consecutive_failures"] == 0
        assert (loop["needs_you"]["reason"], loop["needs_you"]["offers"]) == ("issue_attempts", ["dispatch", "cancel"])
        assert world.issue()["status"] == "open"
        await command(world, "dispatch", "c5")  # the user overrides the per-issue cap
        loop = world.loop()
        assert len(loop["attempts"]) == 4 and loop["attempts"][3]["override"] is True and not any(a["override"] for a in loop["attempts"][:3])
        assert world.fx.summary(loop)["attempt"]["of"] == 4 and loop["needs_you"]["reason"] == "usage_limit"
        await command(world, "dispatch", "c6")  # the override was used up
        assert len(world.loop()["attempts"]) == 4 and world.loop()["needs_you"]["reason"] == "issue_attempts"


@pytest.mark.asyncio
async def test_repeated_counted_failures_stop_before_the_per_issue_cap(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        world.cycle.tests = [1]
        world.cycle.jr = [DONE]
        await world.hit()
        loop = world.loop()
        assert len(loop["attempts"]) == 3 and loop["consecutive_failures"] == 3
        assert (loop["needs_you"]["reason"], loop["needs_you"]["offers"]) == ("repeat_failures", ["cancel"])
        with pytest.raises(ValueError, match="not available"):
            await world.fx.command(world.loop_id, "dispatch", "c-refused")


@pytest.mark.asyncio
@pytest.mark.parametrize("count, merged, reason", [(8, False, "loop_attempts"), (5, True, "loop_fixes")])
async def test_loop_caps_on_attempts_and_merged_fixes(count, merged, reason, environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile, mode="manual") as world:
        await world.hit()
        seed_attempts(world, count, **({"merge_sha": "9" * 40, "result": "verified"} if merged else {}))
        await command(world, "dispatch", "c1")
        loop = world.loop()
        assert (loop["needs_you"]["reason"], loop["needs_you"]["offers"]) == (reason, ["cancel"])
        assert len(loop["attempts"]) == count and world.calls("jr") == [] and world.issue()["status"] == "open"
        assert world.fx.summary(loop)["merged_fixes"] == (count if merged else 0)
        with pytest.raises(ValueError, match="not available"):
            await world.fx.command(world.loop_id, "dispatch", "c2")


@pytest.mark.asyncio
async def test_daily_budget(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile, mode="manual") as world:
        world.cycle.jr = [USAGE]
        await world.hit()
        key = world.fx._usage_key()
        world.store.put("fixer_usage", {"id": key, "minutes": 240})
        await command(world, "dispatch", "c1")
        loop = world.loop()
        assert (loop["needs_you"]["reason"], loop["needs_you"]["offers"]) == ("budget", ["dispatch", "cancel"])
        assert loop["attempts"] == [] and world.calls("jr") == [] and world.issue()["status"] == "open"
        assert world.fx.summary(loop)["minutes_today"] == 240 and world.fx.summary(loop)["daily_minutes"] == 240
        world.store.put("fixer_usage", {"id": key, "minutes": 230})
        await command(world, "dispatch", "c2")
        [call] = [c for c in world.shell.calls if c.argv[0] == "jr"]
        assert call.timeout == 600
        assert world.loop()["attempts"][0]["minutes"] == 1 and world.fx.minutes_today() == 231
        world.store.put("settings", {**world.store.require("settings", "main"), "fixer_daily_minutes": 231})
        await command(world, "dispatch", "c3")  # usage_limit offers dispatch, but the raised-then-reached budget still applies
        assert world.loop()["needs_you"]["reason"] == "budget"


@pytest.mark.asyncio
async def test_dirty_checkout_needs_you(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        world.cycle.porcelain = " M ashad/ai.py\n"
        await world.hit()
        loop = world.loop()
        assert (loop["needs_you"]["reason"], loop["needs_you"]["offers"]) == ("checkout_dirty", ["apply", "cancel"])
        assert str(world.checkout) in loop["needs_you"]["message"] and world.calls("git", "pull") == []
        merges = len(world.calls("gh", "pr", "merge"))
        world.cycle.porcelain = ""
        await command(world, "apply", "c1")
        assert world.loop()["phase"] == "restart_required" and len(world.calls("git", "pull")) == 1
        assert len(world.calls("gh", "pr", "merge")) == merges == 1  # apply retried only the apply step


@pytest.mark.asyncio
async def test_dirty_checkout_on_another_branch(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        world.cycle.branch = "feature"
        await world.hit()
        assert world.loop()["needs_you"]["reason"] == "checkout_dirty" and world.calls("git", "pull") == []
        world.cycle.pull_rc = 1
        world.cycle.branch = "main"
        await command(world, "apply", "c1")  # a failed fast-forward is still a checkout problem
        assert world.loop()["needs_you"]["reason"] == "checkout_dirty"


@pytest.mark.asyncio
async def test_packaged_launch_needs_manual_restart(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        async def pull(call):
            world.fx.checkout_root = None  # a launch without a checkout cannot restart ASHAD itself
            return 0, ""
        world.shell.script.insert(0, (("git", "pull"), pull))
        await world.hit()
        loop = world.loop()
        assert (loop["phase"], loop["needs_you"]["reason"], loop["needs_you"]["offers"]) == ("needs_you", "restart_needed", ["cancel"])
        assert "git pull --ff-only origin main" in loop["needs_you"]["message"] and world.fx.restart_for_fix() is None
        assert loop["attempts"][0]["merge_sha"] == world.cycle.merge


@pytest.mark.asyncio
async def test_stale_runtime_after_restart_needs_you(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        world.cycle.diff = "M\tashad/structured.py\nA\ttests/test_new_verb.py\n"
        await world.hit()
        assert world.loop()["phase"] == "restart_required"
        (tmp_path / "runtime/ashad/structured.py").write_text("older code still running")
        runs = len(world.store.list("runs"))
        world.restart()
        await world.coord.fixer.recover()
        await world.settle()
        loop = world.loop()
        assert (loop["phase"], loop["needs_you"]["reason"], loop["needs_you"]["offers"]) == ("needs_you", "restart_needed", ["cancel"])
        assert len(world.store.list("runs")) == runs and loop["verify"] is None and world.fx.restart_for_fix() is None
        assert world.store.require("runs", world.env.rid)["state"] == "Fixing"
        assert "npm --prefix desktop run prepare:runtime" in loop["needs_you"]["message"]


def in_order(world, *prefixes, start=0):
    """Index of the call matching each prefix, searched in order from `start`; fails when one is missing or out of order."""
    found, index = [], start
    for prefix in prefixes:
        while index < len(world.shell.calls) and FakeShell.key(world.shell.calls[index].argv)[:len(prefix)] != tuple(prefix):
            index += 1
        assert index < len(world.shell.calls), f"{prefix} not found in order after {found}"
        found.append(index)
        index += 1
    return found


def pass_verification(world, verb="interact", kind="chest", action_id="act-1"):
    world.verify_events(
        ("structured_action_intent", {"player_id": PLAYER, "action_id": action_id, "target_kind": kind,
                                      "input": {"verb": verb, "target_id": "chest-1"}}),
        ("action_result", {"player_id": PLAYER, "action_id": action_id, "result": {"status": "completed"}}))


@pytest.mark.asyncio
async def test_verify_fail_reverts_and_retries_in_auto(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        world.cycle.jr = [DONE, USAGE]
        vid = await world.verifying()
        first = world.loop()["attempts"][0]
        start = len(world.shell.calls)
        world.direct_hit(run_id=vid, basis_=world.issue()["signature_basis"])  # the same issue came back
        await world.settle()
        assert world.store.require("runs", vid)["state"] == "Fixing"
        loop = world.loop()
        assert loop["phase"] == "restart_required" and loop["attempts"][0]["result"] == "verify_failed" and loop["consecutive_failures"] == 1
        revert = loop["attempts"][0]["revert"]
        assert revert["reason"] == "verify_failed" and revert["pr"]["number"] == 8 and revert["merge_sha"] == world.cycle.revert_merge
        assert revert["branch"].startswith(f"revert/ashad-{first['id']}-")
        wt = revert["worktree"]
        in_order(world,
                 ("git", "revert", "-m", "1", world.cycle.merge, "--no-edit"), ("git", "push", "origin", f"HEAD:refs/heads/{revert['branch']}"),
                 ("gh", "pr", "create"), ("gh", "pr", "list"), ("git", "fetch", "origin", "pull/8/head"), ("git", "diff"),
                 ("scripts/test.sh",), ("gh", "pr", "merge", "8", "--repo", fixer.FIX_REPO, "--merge", "--match-head-commit", world.cycle.revert_head),
                 ("git", "rev-list"), ("git", "pull"), ("git", "worktree", "remove"), start=start)
        [create] = world.calls("gh", "pr", "create")
        assert flag_value(create, "--title").endswith(f"[ashad-revert {first['id']}]") and flag_value(create, "--head") == revert["branch"]
        assert f"merge {world.cycle.merge}" in flag_value(create, "--body") and "verify_failed" in flag_value(create, "--body")
        assert [c.argv for c in world.shell.calls[start:] if c.argv[:2] == ["git", "-C"]][0][2] == str(world.checkout)
        await world.apply_and_restart()
        issue, loop = world.issue(), world.loop()
        assert issue["fix"]["reverted"]["reason"] == "verify_failed" and issue["fix"]["reverted"]["sha"] == world.cycle.revert_merge
        history = [(h["to"], h["attempt_id"]) for h in issue["status_history"]]
        assert ("open", first["id"]) in history and history[-1][0] == "fixing" and issue["status"] == "fixing"  # attempt 2 took it again
        assert len(loop["attempts"]) == 2 and loop["attempts"][1]["n"] == 2 and loop["needs_you"]["reason"] == "usage_limit"
        assert loop["attempts"][0]["revert"]["applied_at"] and [e["payload"]["reason"] for e in events(world.env, "fix_reverted", vid)] == ["verify_failed"]


@pytest.mark.asyncio
@pytest.mark.parametrize("found", [{"isCrossRepository": True}, {"baseRefName": "dev"}, {"state": "CLOSED"}])
async def test_revert_pr_lookup_refuses_a_fork_wrong_base_or_closed_pr(found, environment, monkeypatch, tmp_path, profile):
    """Only a same-repo open PR into main on the revert branch is gated and merged; a fork's PR with that head name never is."""
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        vid = await world.verifying()

        async def listing(call):
            if not flag_value(call.argv, "--head").startswith("revert/"):
                return await world.cycle.on_list(call)
            pr = {"number": 9, "url": PR_URL.format(9), "state": "OPEN", "headRefOid": "f" * 40, "baseRefName": "main",
                  "isCrossRepository": False, **found}
            return 0, json.dumps([pr])
        world.shell.script.insert(0, (("gh", "pr", "list"), listing))
        world.direct_hit(run_id=vid, basis_=world.issue()["signature_basis"])
        await world.settle()
        loop = world.loop()
        assert (loop["phase"], loop["needs_you"]["reason"]) == ("needs_you", "revert_failed")
        assert loop["attempts"][0]["revert"]["pr"] is None
        assert [argv[3] for argv in world.calls("gh", "pr", "merge")] == ["7"]
        assert world.calls("git", "fetch", "origin", "pull/9/head") == []


@pytest.mark.asyncio
async def test_cancel_after_an_unrecorded_merge_notes_the_merged_pr(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        hold = await hold_until_started(world, ("gh", "pr", "merge"))
        await world.until(lambda l: l["phase"] == "merging")
        world.cycle.user_merges(world.cycle.pr)  # GitHub merged it; ASHAD is cancelled before it reads the merge back
        await asyncio.wait_for(world.fx.command(world.loop_id, "cancel", "c1"), 20)
        loop = world.loop()
        assert loop["phase"] == "ended" and hold.cancelled and world.calls("gh", "pr", "close") == []
        assert loop["cancel_notes"] == ["PR #7 was already merged. Revert it on GitHub if you do not want it on main."]


@pytest.mark.asyncio
async def test_manual_verify_fail_offers_revert_and_dispatch(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile, mode="manual") as world:
        await world.hit()
        await command(world, "dispatch", "c1")
        world.cycle.user_merges(7)
        await command(world, "apply", "c2")
        await world.apply_and_restart()
        vid = world.verify_run_id = world.loop()["verify"]["run_id"]
        world.set_run(state="Running")
        calls = len(world.shell.calls)
        world.direct_hit(run_id=vid, basis_=world.issue()["signature_basis"])
        await world.settle()
        loop = world.loop()
        assert (loop["phase"], loop["needs_you"]["reason"], loop["needs_you"]["offers"]) == ("needs_you", "verify_failed", ["revert", "dispatch", "cancel"])
        assert len(world.shell.calls) == calls and world.store.require("runs", vid)["state"] == "Fixing"  # no git command, run stopped
        assert loop["attempts"][0]["result"] == "verify_failed" and loop["attempts"][0]["revert"] is None


@pytest.mark.asyncio
async def test_no_recurrence_window_passes(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        vid = await world.verifying()
        monkeypatch.setattr(fixer, "VERIFY_RUNNING_SECONDS", 0.3)
        world.set_run(state="Paused")
        await asyncio.sleep(0.6)  # twice the window, but not a second of it was spent Running
        verify = world.loop()["verify"]
        assert world.loop()["phase"] == "verifying" and verify["running_seconds"] == 0
        monkeypatch.setattr(fixer, "VERIFY_RUNNING_SECONDS", 900)
        world.verify_events(*[("decision_selected", {"player_id": PLAYER})] * (fixer.VERIFY_DECISIONS - 1))
        await asyncio.sleep(0.3)
        assert world.loop()["phase"] == "verifying" and world.loop()["verify"]["decisions"] == fixer.VERIFY_DECISIONS - 1
        world.verify_events(("decision_selected", {"player_id": PLAYER}))
        await world.until(lambda l: l["phase"] == "playing")
        assert world.issue()["status"] == "fixed" and world.issue()["fix"]["verified"]["pass"] == "no_recurrence"
        assert world.loop()["attempts"][0]["result"] == "verified" and world.loop()["verify"] is None


@pytest.mark.asyncio
async def test_no_recurrence_by_running_time(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        monkeypatch.setattr(fixer, "VERIFY_RUNNING_SECONDS", 0.3)
        await world.verifying()
        await world.until(lambda l: l["phase"] == "playing")
        assert world.issue()["fix"]["verified"]["pass"] == "no_recurrence"


@pytest.mark.asyncio
async def test_other_verbs_pass_only_by_no_recurrence(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        await world.verifying()
        pass_verification(world, verb="dance", kind="floor")  # another verb completing is not the wanted action
        await asyncio.sleep(0.3)
        assert world.loop()["phase"] == "verifying"
        pass_verification(world, verb="interact", kind="door", action_id="act-2")  # wanted verb, wrong target kind
        await asyncio.sleep(0.3)
        assert world.loop()["phase"] == "verifying"


@pytest.mark.asyncio
async def test_superseded_by_new_issue(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        world.cycle.jr = [DONE, USAGE]
        vid = await world.verifying()
        old_id = world.loop()["issue"]["issue_id"]
        new_id = world.direct_hit(run_id=vid, basis_=basis(verb="ui_click", target_kind="button", target_label="ok #"))
        await world.settle()
        old, new, loop = world.store.get("issues", old_id), world.store.get("issues", new_id), world.loop()
        assert old["status"] == "fixed" and old["fix"]["verified"]["pass"] == "superseded" and old["fix"]["merge_sha"] == world.cycle.merge
        assert new["status"] == "fixing" and loop["issue"]["issue_id"] == new_id
        assert loop["attempts"][0]["result"] == "superseded" and loop["consecutive_failures"] == 0
        assert len(loop["attempts"]) == 2 and loop["attempts"][1]["issue_id"] == new_id and world.store.require("runs", vid)["state"] == "Fixing"


@pytest.mark.asyncio
@pytest.mark.parametrize("category", sorted(fixer.REGRESSION_CATEGORIES))
async def test_regression_categories_revert_and_pause(category, environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        world.cycle.jr = [DONE]
        vid = await world.verifying()
        world.set_run(state="Investigating", pause={"category": category})
        await world.until(lambda l: l["phase"] == "restart_required")
        assert world.store.require("runs", vid)["state"] == "Fixing" and world.loop()["attempts"][0]["revert"]["reason"] == "regression"
        await world.apply_and_restart()
        loop = world.loop()
        assert (loop["phase"], loop["needs_you"]["reason"], loop["needs_you"]["offers"]) == ("needs_you", "regression", ["dispatch", "cancel"])
        assert len(loop["attempts"]) == 1 and len(world.calls("jr")) == 1 and loop["consecutive_failures"] == 0
        assert world.issue()["status"] == "open" and world.issue()["fix"]["reverted"]["reason"] == "regression"


@pytest.mark.asyncio
async def test_unrelated_investigating_pause_is_not_a_regression(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        vid = await world.verifying()
        world.set_run(state="Investigating", pause={"category": "agent_needs_instructions"})
        await asyncio.sleep(0.3)
        assert world.loop()["phase"] == "verifying" and world.store.require("runs", vid)["state"] == "Investigating"
        world.set_run(state="Ended")
        await world.settle()
        await world.until(lambda l: l["phase"] == "needs_you")
        assert world.loop()["needs_you"]["reason"] == "verify_inconclusive"
        assert world.loop()["needs_you"]["offers"] == ["revert", "cancel"]


async def verified(world):
    """Auto cycle, restart, a passed verification: the loop is `playing` again on the check run."""
    vid = await world.verifying()
    pass_verification(world)
    await world.until(lambda l: l["phase"] == "playing")
    return vid


@pytest.mark.asyncio
async def test_fixed_issue_recurring_later_reverts_its_fix(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        world.cycle.jr = [DONE]
        vid = await verified(world)
        assert world.issue()["status"] == "fixed"
        world.direct_hit(run_id=vid, basis_=world.issue()["signature_basis"])  # a later run of the same loop hits the fixed issue
        await world.until(lambda l: l["phase"] == "restart_required")
        [revert] = world.calls("git", "revert")
        assert revert[3:] == ["revert", "-m", "1", world.cycle.merge, "--no-edit"]
        assert world.store.require("runs", vid)["state"] == "Fixing"
        await world.apply_and_restart()
        loop = world.loop()
        assert (loop["needs_you"]["reason"], loop["needs_you"]["offers"]) == ("regression", ["dispatch", "cancel"])
        assert loop["attempts"][0]["result"] == "verified" and loop["attempts"][0]["revert"]["reason"] == "regression"
        assert world.issue()["status"] == "open" and len(loop["attempts"]) == 1 and len(world.calls("jr")) == 1


@pytest.mark.asyncio
async def test_fixed_issue_from_another_loop_only_offers_cancel(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        record_issue(world.store, "unsupported_action", basis(), example())
        issue_id = repair_tasks.issue_id("unsupported_action", basis())
        set_issue_status(world.store, issue_id, "fixed", loop_id="loop_old", attempt_id="fix_old",
                         fix={"attempt_id": "fix_old", "merge_sha": "1" * 40})
        world.direct_hit()
        await world.settle()
        loop = world.loop()
        assert (loop["needs_you"]["reason"], loop["needs_you"]["offers"]) == ("regression", ["cancel"])
        assert "1" * 40 in loop["needs_you"]["message"] and world.shell.calls == []


@pytest.mark.asyncio
async def test_post_merge_tests_failure_is_regression(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        world.cycle.tests = [0, 1, 0]  # gate passes, scripts/test.sh on the merge commit fails, the revert passes
        world.cycle.jr = [DONE]
        await world.hit()
        await world.until(lambda l: l["phase"] == "restart_required")
        loop = world.loop()
        assert loop["attempts"][0]["merge_sha"] == world.cycle.merge and loop["attempts"][0]["revert"]["reason"] == "regression"
        assert world.calls("git", "pull")[0][2] == str(world.checkout) and len(world.calls("git", "pull")) == 1  # only the revert was pulled
        in_order(world, ("scripts/test.sh",), ("gh", "pr", "merge", "7"), ("scripts/test.sh",), ("git", "revert"), ("gh", "pr", "merge", "8"))
        await world.apply_and_restart()
        loop = world.loop()
        assert (loop["needs_you"]["reason"], loop["needs_you"]["offers"]) == ("regression", ["dispatch", "cancel"])
        assert world.issue()["status"] == "open" and len(loop["attempts"]) == 1 and loop["verify"] is None


@pytest.mark.asyncio
async def test_command_rules(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile, mode="manual") as world:
        world.cycle.jr_gate = asyncio.Event()
        world.cycle.jr = [USAGE]
        await world.hit()
        for refused in ("apply", "revert", "bogus"):  # apply is only offered in review_pr / review_revert / checkout_dirty
            with pytest.raises(ValueError, match="not available|Unknown loop command"):
                await world.fx.command(world.loop_id, refused, "c-" + refused)
        assert world.fx.store.command("loop", "c-apply") is None and world.shell.calls == []
        summary = await world.fx.command(world.loop_id, "dispatch", "c-dispatch")
        assert summary["id"] == world.loop_id and not world.fx.tasks[world.loop_id].done()
        await asyncio.sleep(0.1)
        assert [c.argv[0] for c in world.shell.calls].count("jr") == 1
        with pytest.raises(ValueError, match="The fixer is busy with this loop. Wait or cancel."):
            await world.fx.command(world.loop_id, "dispatch", "c-busy")
        with pytest.raises(ValueError, match="busy"):
            await world.fx.command(world.loop_id, "apply", "c-busy-apply")
        calls = len(world.shell.calls)
        again = await world.fx.command(world.loop_id, "dispatch", "c-dispatch")  # the same command id replays without a new attempt
        assert again["id"] == world.loop_id and len(world.shell.calls) == calls and len(world.loop()["attempts"]) == 1
        with pytest.raises(ValueError, match="different command"):
            await world.fx.command(world.loop_id, "revert", "c-dispatch")
        world.cycle.jr_gate.set()
        await world.settle()
        assert world.loop()["needs_you"]["reason"] == "usage_limit"
        loop = world.loop()
        loop["phase"] = "ended"
        world.store.put("loops", loop)
        with pytest.raises(ValueError, match="ended"):
            await world.fx.command(world.loop_id, "cancel", "c-cancel-ended")
        with pytest.raises(ValueError, match="ended"):
            await world.fx.command(world.loop_id, "dispatch", "c-dispatch-ended")
        with pytest.raises(ValueError, match="Unknown loop"):
            await world.fx.command("loop_missing", "dispatch", "c-missing")


def completed_progress(run, quests, goals_done):
    """The run's own progress and plan with `quests` completed quests and `goals_done` completed goals (plus the open root)."""
    goals = [{**run["subgoal_plan"]["goals"][0], "id": f"g{i}", "status": "completed"} for i in range(goals_done)]
    return {"progress": {**(run.get("progress") or {}), "completed_quests": [f"q{i}" for i in range(quests)]},
            "subgoal_plan": {**run["subgoal_plan"], "goals": [run["subgoal_plan"]["goals"][0], *goals]}}


@pytest.mark.asyncio
async def test_progress_rows_and_further(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        world.set_run(world.env.rid, **completed_progress(world.store.require("runs", world.env.rid), 1, 1))
        world.cycle.jr = [DONE]
        vid = await verified(world)
        first_attempt = world.loop()["attempts"][0]["id"]
        [first] = world.loop()["progress"]
        stored = world.store.require("runs", world.env.rid)
        assert (first["run_id"], first["quests"], first["goals_completed"], first["fix_before"], first["further"]) == (world.env.rid, 1, 1, None, None)
        assert first["action_kinds"] == len(next(iter(stored["players"].values())).get("attempted_actions", []))
        assert first["stopped_by"] == world.issue()["title"] and world.loop()["phase"] == "playing"
        world.set_run(vid, **completed_progress(world.store.require("runs", vid), 2, 2))
        await world.coord.finish(vid, "user_stopped")
        rows = world.loop()["progress"]
        assert [r["run_id"] for r in rows] == [world.env.rid, vid]
        assert (rows[1]["quests"], rows[1]["goals_completed"], rows[1]["fix_before"], rows[1]["further"]) == (2, 2, first_attempt, "more")
        assert rows[1]["stopped_by"] == "user_stopped" and world.loop()["phase"] == "ended"
        check = world.store.require("runs", vid)
        for name, extra, expected in (("same", completed_progress(check, 2, 2), "same"), ("less", completed_progress(check, 1, 2), "less")):
            world.fx.run_finished({**check, "id": "run_" + name, **extra})
            assert world.loop()["progress"][-1]["further"] == expected and world.loop()["progress"][-1]["run_id"] == "run_" + name


def test_recover_without_checkout_runs_no_command(tmp_path):
    shell = FakeShell()
    instance = make_fixer(tmp_path, shell)
    loop = instance.create_loop("auto")
    attempt = stub_attempt(1, "issue_x", merge_sha="9" * 40, result=None)
    instance.store.put("loops", {**loop, "phase": "restart_required", "attempts": [attempt], "current_run_id": None})

    async def recover():
        await instance.recover()
        await asyncio.gather(*instance.tasks.values())

    asyncio.run(recover())
    assert shell.calls == [] and instance.store.get("loops", loop["id"])["needs_you"]["reason"] == "restart_needed"


# ---- cancel, quit and recovery ----------------------------------------------------------------------------------------

class Hold:
    """A shell handler that signals it was entered, then sleeps until cancelled; `cancelled` records the CancelledError."""

    def __init__(self):
        self.started, self.cancelled = asyncio.Event(), []

    async def __call__(self, call):
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled.append(call.argv[0])
            raise


async def hold_until_started(world, prefix, trigger=None):
    """Make the first call matching `prefix` hang, start the loop's work with `trigger` and wait until it is inside that call."""
    hold = Hold()
    world.shell.script.insert(0, (prefix, hold))
    result = (trigger or (lambda: act(world.env)))()
    if asyncio.iscoroutine(result):
        await result
    await asyncio.wait_for(hold.started.wait(), 20)
    return hold


async def crash(world, hold, prefix):
    """A hard stop at the held call: the driver task dies without cleanup and the phase stays, as after a killed controller."""
    task = world.fx.tasks[world.loop_id]
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    assert hold.cancelled
    world.shell.script.remove((prefix, hold))


async def recovered(world):
    """A new controller over the same store runs the real Coordinator.recover and the loop's recovery task finishes or parks."""
    for task in list(world.fx.tasks.values()):
        task.cancel()
    await asyncio.gather(*world.fx.tasks.values(), return_exceptions=True)
    world.restart()
    await world.coord.recover()
    await world.settle()


CANCEL_PHASES = {"fixing": 0, "gating": 1, "merging": 1, "needs_you": 1, "restart_required": 0, "verifying": 0}  # phase -> PRs closed


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", list(CANCEL_PHASES))
async def test_cancel_always_works(phase, environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile, mode="manual" if phase == "needs_you" else "auto") as world:
        hold = None
        if phase in ("fixing", "gating", "merging"):
            prefix = {"fixing": ("jr", "ship"), "gating": ("scripts/test.sh",), "merging": ("gh", "pr", "merge")}[phase]
            hold = await hold_until_started(world, prefix)
            await world.until(lambda l: l["phase"] == phase)
        elif phase == "needs_you":
            await world.hit()
            await command(world, "dispatch", "c-dispatch")
            assert world.loop()["needs_you"]["reason"] == "review_pr"
        elif phase == "restart_required":
            await world.hit()
        else:
            await world.verifying()
        assert world.loop()["phase"] == ("verifying" if phase == "verifying" else phase)
        removes = len(world.calls("git", "worktree", "remove"))
        summary = await asyncio.wait_for(world.fx.command(world.loop_id, "cancel", "c1"), 20)
        loop = world.loop()
        assert summary["phase"] == loop["phase"] == "ended" and loop["needs_you"] is None and loop["cancel_notes"] == []
        assert hold is None or hold.cancelled == [hold_name(phase)]
        assert world.fx.tasks[world.loop_id].done() and world.fx.in_flight() == []
        assert len(world.calls("gh", "pr", "close")) == CANCEL_PHASES[phase]
        assert all(flag_value(argv, "--comment") == "Cancelled in ASHAD." for argv in world.calls("gh", "pr", "close"))
        if phase not in ("restart_required", "verifying"):  # in those the apply step already removed the worktree
            assert len(world.calls("git", "worktree", "remove")) == removes + 1
        assert not fixer.Path(loop["attempts"][0]["worktree"]).exists()
        if phase == "verifying":
            assert world.store.require("runs", loop["verify"]["run_id"])["state"] == "Running"  # the check run keeps going
            assert world.store.require("runs", world.env.rid)["ending_reason"] == "fix_applied"
        else:
            fixing = world.store.require("runs", world.env.rid)
            assert (fixing["state"], fixing["ending_reason"]) == ("Ended", "fix_cancelled")
        assert loop["attempts"][0]["result"] == "cancelled" and loop["consecutive_failures"] == 0
        assert world.issue()["status"] == "open"
        assert world.calls("git", "revert") == [] and world.calls("gh", "pr", "create") == []
        assert len(world.calls("gh", "pr", "merge")) == (1 if phase in ("merging", "restart_required", "verifying") else 0)
        assert events(world.env, "fix_cancelled", loop["current_run_id"])
        calls = len(world.shell.calls)
        again = await world.fx.command(world.loop_id, "cancel", "c1")
        assert again["phase"] == "ended" and len(world.shell.calls) == calls


def hold_name(phase):
    return "scripts/test.sh" if phase == "gating" else "gh" if phase == "merging" else "jr"


@pytest.mark.asyncio
async def test_cancel_keeps_going_after_a_failed_step(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        hold = await hold_until_started(world, ("gh", "pr", "merge"))
        await world.until(lambda l: l["phase"] == "merging")

        async def broken(call):
            raise RuntimeError("secret path /home/x token")
        world.shell.script.insert(0, (("gh", "pr", "view"), broken))
        await asyncio.wait_for(world.fx.command(world.loop_id, "cancel", "c1"), 20)
        loop = world.loop()
        assert loop["phase"] == "ended" and loop["cancel_notes"] == ["Cancel step failed: close the fix PR."]
        assert "secret" not in json.dumps(loop) and hold.cancelled
        assert world.store.require("runs", world.env.rid)["ending_reason"] == "fix_cancelled" and world.issue()["status"] == "open"


@pytest.mark.asyncio
async def test_cancel_closes_an_unfinished_revert_pr_but_never_reverts_again(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        world.cycle.jr = [DONE]
        vid = await world.verifying()
        hold = await hold_until_started(world, ("gh", "pr", "merge", "8"),
                                        lambda: world.direct_hit(run_id=vid, basis_=world.issue()["signature_basis"]))
        await world.until(lambda l: l["phase"] == "reverting" and l["attempts"][0]["revert"]["pr"])
        await asyncio.wait_for(world.fx.command(world.loop_id, "cancel", "c1"), 20)
        [close] = world.calls("gh", "pr", "close")
        assert close[3] == "8" and hold.cancelled and len(world.calls("git", "revert")) == 1
        loop = world.loop()
        assert loop["phase"] == "ended" and loop["attempts"][0]["result"] == "verify_failed" and loop["attempts"][0]["revert"]["merge_sha"] is None
        assert not fixer.Path(loop["attempts"][0]["revert"]["worktree"]).exists()


@pytest.mark.asyncio
async def test_cancel_prunes_old_attempt_directories(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile, mode="manual") as world:
        await world.hit()
        root = world.store.root / "fixer"
        for n in range(fixer.KEEP_ATTEMPT_DIRS + 3):
            old = root / f"fix_old{n:03d}"
            old.mkdir(parents=True)
            os.utime(old, (1000 + n, 1000 + n))
        await world.fx.command(world.loop_id, "cancel", "c1")
        left = sorted(path.name for path in root.glob("fix_*"))
        assert len(left) == fixer.KEEP_ATTEMPT_DIRS and "fix_old000" not in left and "fix_old002" not in left


@pytest.mark.asyncio
async def test_recover_fixing_with_pr_found_continues_the_gate(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        prefix = ("jr", "ship")
        hold = await hold_until_started(world, prefix)
        await crash(world, hold, prefix)
        assert world.loop()["phase"] == "fixing" and world.loop()["attempts"][0]["result"] is None
        await recovered(world)
        loop = world.loop()
        assert loop["phase"] == "restart_required" and len(loop["attempts"]) == 1 and loop["attempts"][0]["pr"]["number"] == 7
        assert len(world.calls("jr", "ship")) == 1 and len(world.calls("gh", "pr", "merge")) == 1
        assert loop["consecutive_failures"] == 0 and loop["attempts"][0]["merge_sha"] == world.cycle.merge


@pytest.mark.asyncio
async def test_recover_fixing_without_pr_is_interrupted_and_counted_once(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        world.cycle.pr_exists, world.cycle.jr = False, [USAGE]
        prefix = ("jr", "ship")
        hold = await hold_until_started(world, prefix)
        await crash(world, hold, prefix)
        await recovered(world)
        loop = world.loop()
        assert [a["result"] for a in loop["attempts"]] == ["interrupted", "usage_limit"] and loop["consecutive_failures"] == 1
        assert loop["needs_you"]["reason"] == "usage_limit" and world.calls("gh", "pr", "merge") == []


@pytest.mark.asyncio
async def test_recover_fixing_before_any_attempt_resumes_the_start(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile, mode="manual") as world:
        await world.hit()
        loop = world.loop()
        loop.update(phase="fixing", needs_you=None)  # killed between the stop and the first attempt
        world.store.put("loops", loop)
        set_issue_status(world.store, world.issue()["id"], "open", loop_id=world.loop_id, attempt_id=None)
        await recovered(world)
        assert world.loop()["needs_you"]["reason"] == "dispatch" and world.issue()["status"] == "fixing" and world.shell.calls == []


@pytest.mark.asyncio
async def test_recover_gating_runs_the_gate_again_once(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        prefix = ("scripts/test.sh",)
        hold = await hold_until_started(world, prefix)
        await crash(world, hold, prefix)
        assert world.loop()["phase"] == "gating"
        await recovered(world)
        assert world.loop()["phase"] == "restart_required" and len(world.calls("gh", "pr", "merge")) == 1
        assert len(world.calls("jr", "ship")) == 1


MERGE_RECOVERY = [  # id, PR merged on GitHub, head sha on GitHub changed, merges after recovery, attempt result, phase
    ("merged", True, False, 1, None, "restart_required"),
    ("open_same_head", False, False, 2, None, "restart_required"),
    ("open_other_head", False, True, 1, "merge_failed", "needs_you")]


@pytest.mark.asyncio
@pytest.mark.parametrize("case", MERGE_RECOVERY, ids=[case[0] for case in MERGE_RECOVERY])
async def test_recover_merging_reads_the_pr_before_merging(case, environment, monkeypatch, tmp_path, profile):
    _, merged, other_head, merges, result, phase = case
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        world.cycle.jr = [DONE, USAGE]
        prefix = ("gh", "pr", "merge")
        hold = await hold_until_started(world, prefix)
        await crash(world, hold, prefix)
        assert world.loop()["phase"] == "merging"
        if merged:
            world.cycle.user_merges(7)
        if other_head:
            world.cycle.head = "9" * 40
        await recovered(world)
        loop = world.loop()
        assert len(world.calls("gh", "pr", "merge")) == merges and loop["attempts"][0]["result"] == result
        assert loop["phase"] == phase if result is None else loop["needs_you"]["reason"] == "usage_limit"
        if result is None:
            assert loop["attempts"][0]["merge_sha"] == world.cycle.merge and loop["consecutive_failures"] == 0
        else:
            assert len(world.calls("gh", "pr", "close")) == 1 and loop["consecutive_failures"] == 1 and len(loop["attempts"]) == 2
        assert all(argv[-1] == "a" * 40 for argv in world.calls("gh", "pr", "merge"))  # the same --match-head-commit every time


@pytest.mark.asyncio
@pytest.mark.parametrize("worktree_left", [True, False], ids=["worktree_left", "worktree_removed"])
async def test_recover_applying_repeats_only_the_apply(worktree_left, environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        prefix = ("git", "pull")
        hold = await hold_until_started(world, prefix)
        await crash(world, hold, prefix)
        assert world.loop()["phase"] == "applying"
        if not worktree_left:
            shutil.rmtree(world.loop()["attempts"][0]["worktree"])  # killed after the worktree was removed: D15 already passed
        tests = len(world.calls("scripts/test.sh"))
        await recovered(world)
        assert world.loop()["phase"] == "restart_required" and len(world.calls("gh", "pr", "merge")) == 1
        assert len(world.calls("scripts/test.sh")) == tests + (1 if worktree_left else 0)
        assert world.loop()["attempts"][0]["result"] is None and len(world.calls("git", "pull")) == 2


async def revert_crash(world, prefix):
    """Auto, verifying, the issue recurs: the revert starts and the controller dies inside the call matching `prefix`."""
    world.cycle.jr = [DONE]
    vid = await world.verifying()
    hold = await hold_until_started(world, prefix, lambda: world.direct_hit(run_id=vid, basis_=world.issue()["signature_basis"]))
    await crash(world, hold, prefix)
    assert world.loop()["phase"] == "reverting"
    return world.loop()["attempts"][0]["revert"]


@pytest.mark.asyncio
async def test_recover_reverting_with_a_merged_revert_pr_does_not_revert_again(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        revert = await revert_crash(world, ("gh", "pr", "merge", "8"))
        assert revert["pr"]["number"] == 8
        world.cycle.user_merges(8)
        await recovered(world)
        loop = world.loop()
        assert loop["phase"] == "restart_required" and loop["attempts"][0]["revert"]["merge_sha"] == world.cycle.revert_merge
        assert len(world.calls("git", "revert")) == 1 and len(world.calls("gh", "pr", "create")) == 1
        assert len(world.calls("gh", "pr", "merge")) == 2 and len(world.calls("git", "push")) == 1


@pytest.mark.asyncio
async def test_recover_reverting_with_an_open_revert_pr_gates_and_merges_it_once(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        await revert_crash(world, ("gh", "pr", "merge", "8"))
        await recovered(world)
        assert world.loop()["phase"] == "restart_required" and len(world.calls("git", "revert")) == 1
        assert [argv[3] for argv in world.calls("gh", "pr", "merge")] == ["7", "8", "8"]
        assert {argv[-1] for argv in world.calls("gh", "pr", "merge") if argv[3] == "8"} == {world.cycle.revert_head}


@pytest.mark.asyncio
async def test_recover_reverting_without_pr_but_branch_pushed_creates_the_pr_only(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        revert = await revert_crash(world, ("gh", "pr", "create"))
        assert revert["pr"] is None
        world.cycle.ls_remote_rc = 0
        await recovered(world)
        loop = world.loop()
        assert loop["phase"] == "restart_required" and loop["attempts"][0]["revert"]["pr"]["number"] == 8
        assert len(world.calls("git", "revert")) == 1 and len(world.calls("git", "push")) == 1 and len(world.calls("gh", "pr", "create")) == 2
        [ls] = world.calls("git", "ls-remote")
        assert ls[3:] == ["ls-remote", "--exit-code", "--heads", "origin", revert["branch"]]
        titles = {flag_value(argv, "--title") for argv in world.calls("gh", "pr", "create")}
        assert len(titles) == 1 and len({flag_value(argv, "--body") for argv in world.calls("gh", "pr", "create")}) == 1


@pytest.mark.asyncio
async def test_recover_reverting_without_pr_or_branch_restarts_from_the_worktree(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        revert = await revert_crash(world, ("git", "push"))
        assert fixer.Path(revert["worktree"]).exists()
        start = len(world.shell.calls)
        await recovered(world)
        loop = world.loop()
        assert loop["phase"] == "restart_required" and loop["attempts"][0]["revert"]["pr"]["number"] == 8
        in_order(world, ("git", "worktree", "remove", "--force", revert["worktree"]), ("git", "worktree", "add", "--detach", revert["worktree"]),
                 start=start)  # the stale revert worktree went first
        assert len(world.calls("git", "revert")) == 2 and len(world.calls("git", "push")) == 2 and len(world.calls("gh", "pr", "create")) == 1
        assert loop["attempts"][0]["revert"]["branch"] == revert["branch"]


@pytest.mark.asyncio
async def test_recover_reverting_before_the_revert_record_exists(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        world.cycle.jr = [DONE]
        vid = await world.verifying()
        loop = world.loop()
        loop["attempts"][0].update(result="verify_failed")
        loop.update(phase="reverting", verify=None)  # the phase was saved, the process died before _revert wrote its record
        world.store.put("loops", loop)
        world.set_run(vid, state="Fixing")
        await recovered(world)
        loop = world.loop()
        assert loop["phase"] == "restart_required" and loop["attempts"][0]["revert"]["reason"] == "verify_failed"
        assert len(world.calls("git", "revert")) == 1


@pytest.mark.asyncio
async def test_recover_reverting_with_a_closed_revert_pr_needs_you(environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        await revert_crash(world, ("gh", "pr", "merge", "8"))

        async def closed(call):
            return 0, json.dumps({"state": "CLOSED", "mergeCommit": None, "headRefOid": world.cycle.revert_head})
        world.shell.script.insert(0, (("gh", "pr", "view", "8"), closed))
        await recovered(world)
        loop = world.loop()
        assert (loop["phase"], loop["needs_you"]["reason"]) == ("needs_you", "revert_failed") and len(world.calls("git", "revert")) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("start", ["restart_required", "verifying"])
async def test_recover_restart_and_verifying_launch_one_check_run(start, environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile) as world:
        await world.hit()
        assert world.loop()["phase"] == "restart_required"
        await recovered(world)
        first = world.loop()["verify"]["run_id"]
        if start == "restart_required":  # killed after the launch, before the phase was saved
            world.store.put("loops", {**world.loop(), "phase": "restart_required", "verify": None})
        else:
            world.store.put("loops", {**world.loop(), "verify": None})
        await recovered(world)
        await world.until(lambda l: l["verify"])
        loop = world.loop()
        assert loop["phase"] == "verifying" and loop["verify"]["run_id"] == first
        assert [r["id"] for r in world.store.list("runs") if r.get("rerun_kind") == "save_load"] == [first]
        await recovered(world)  # a loop that is already watching just keeps watching
        assert world.loop()["verify"]["run_id"] == first and len(world.store.list("runs")) == 2
        assert world.fx.tasks[world.loop_id].done() is False


async def start_and_quit(world, mode):
    """The fixer is inside jr when the app quits; returns the quit loop."""
    world.cycle.jr_gate = asyncio.Event()
    if mode == "manual":
        await world.hit()
        await world.fx.command(world.loop_id, "dispatch", "c-dispatch")
        await world.until(lambda l: l["phase"] == "fixing")
    else:
        await act(world.env)
    await world.until(lambda l: l["phase"] == "fixing" and l["attempts"])
    while not world.calls("jr", "ship"):
        await asyncio.sleep(0.02)
    await world.fx.cancel_processes()
    return world.loop()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["auto", "manual"])
async def test_quit_interrupts_running_fixer(mode, environment, monkeypatch, tmp_path, profile):
    async with fix_world(environment, monkeypatch, tmp_path, profile, mode=mode) as world:
        loop = await start_and_quit(world, mode)
        assert loop["phase"] == "fixing" and loop["attempts"][0]["result"] == "interrupted" and loop["consecutive_failures"] == 1
        assert world.fx.tasks[world.loop_id].done() and world.fx.in_flight() == [] and loop["attempts"][0]["ended_at"]
        world.cycle.jr_gate, world.cycle.pr_exists, world.cycle.jr = None, False, [USAGE]
        world.restart()
        await world.coord.recover()  # the real Coordinator.recover calls the fixer last
        await world.settle()
        loop = world.loop()
        assert loop["attempts"][0]["result"] == "interrupted" and loop["consecutive_failures"] == 1
        if mode == "auto":
            assert [a["result"] for a in loop["attempts"]] == ["interrupted", "usage_limit"] and loop["attempts"][1]["n"] == 2
            assert loop["needs_you"]["reason"] == "usage_limit"
        else:
            assert len(loop["attempts"]) == 1 and (loop["phase"], loop["needs_you"]["reason"]) == ("needs_you", "attempt_failed")
        assert world.calls("gh", "pr", "merge") == []
