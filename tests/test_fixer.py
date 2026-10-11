"""Improvement loop fixer: fakes and temporary data only; no real gh, jr, claude, network or keyring."""
import asyncio
from collections import namedtuple
import os
import signal
import sys
import time
from types import SimpleNamespace

import keyring.errors
import pytest

from ashad import fixer, repair_tasks
from ashad.claude_backend import TOKEN_REFERENCE
from ashad.fixer import Fixer
from ashad.repair_tasks import issue_directory, record_issue, set_issue_status
from ashad.store import Store
from test_controller import MemoryCredentials
from test_unsupported_actions import basis, example

Call = namedtuple("Call", "argv cwd env timeout log_path")


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


def test_stubs_are_safe_no_ops(tmp_path):
    instance = make_fixer(tmp_path)
    loop = instance.create_loop("auto")
    assert loop["phase"] == "playing" and instance.store.get("loops", loop["id"]) == loop
    assert instance.summary(loop) is loop and instance.issue_hit("r", "unsupported_action", "i", "e", None) is None
    assert instance.run_finished({"id": "r"}) is None and instance.in_flight() == [] and instance.restart_for_fix() is None
    assert asyncio.run(instance.command(loop["id"], "cancel", "c1")) is None
    assert asyncio.run(instance.cancel_processes()) is None and asyncio.run(instance.recover()) is None
    instance.checkout_root = tmp_path
    instance.store.put("loops", {**loop, "phase": "restart_required"})
    assert instance.restart_for_fix() == loop["id"]
    instance._add_minutes(5)
    instance._add_minutes(7)
    assert instance.minutes_today() == 12
