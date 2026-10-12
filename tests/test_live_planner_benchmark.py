"""Live Haiku planner benchmark: acceptance rate of real plans through StructuredPlayers.turn.

Needs CLAUDE_CODE_OAUTH_TOKEN in the environment and ~/.local/bin/claude; the token is never read from the keyring
and Fast mode stays off (ClaudeBackend._environment). Never runs in CI or without the token.
  CLAUDE_CODE_OAUTH_TOKEN=... .venv/bin/python -m pytest tests/test_live_planner_benchmark.py -q -s -p no:cacheprovider
"""
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ashad import subgoals as goals
from ashad.ai import OpenAI
from ashad.claude_backend import TOKEN_REFERENCE, ClaudeBackend
from test_controller import FakeCodexBackend
from test_crash_bug_issues import issue_files
from test_subgoals import runtime

CLI = Path.home() / ".local" / "bin" / "claude"
ROOT = Path(__file__).resolve().parent.parent
INSTRUCTION = "Till tile-1 first, then plant the wheat seeds there"
OBJECTIVES = ["Harvest 10 wheat", "Plant a wheat seed in plot 3"]
INSTRUCTIONS = [None, (INSTRUCTION, "hand_back"), (INSTRUCTION, "resume")]

pytestmark = pytest.mark.skipif(not os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or not CLI.is_file(),
                                reason="needs CLAUDE_CODE_OAUTH_TOKEN and ~/.local/bin/claude")


class EnvCredentials:
    def get(self, reference=None):
        return os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") if reference == TOKEN_REFERENCE else None

    def save(self, *args):
        raise ValueError("The benchmark never stores credentials")

    def remove(self, *args):
        raise ValueError("The benchmark never stores credentials")


def head():
    done = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True)
    return done.stdout.strip() or None


@pytest.mark.asyncio
async def test_haiku_acceptance_rate(tmp_path):
    repeats = int(os.environ.get("ASHAD_PLANNER_BENCH_REPEATS", "1"))
    scenarios = []
    for objective in OBJECTIVES:
        for instruction in INSTRUCTIONS:
            for repeat in range(repeats):
                name = f"{len(scenarios)}"
                store, coordinator, service = runtime(tmp_path / name, goals.new_plan(objective, "p", "run"))
                run = store.require("runs", "run")
                run["model"] = run["plan"]["model"] = "haiku"
                if instruction:
                    run["recovery_instruction"], run["recovery_after"] = instruction
                store.put("runs", run)
                real = OpenAI(store, backend=FakeCodexBackend(),
                              claude_backend=ClaudeBackend(store, EnvCredentials(), binary=[str(CLI)]))
                coordinator.plan_response = lambda context: real.plan_subgoals(context, "run")
                await service.turn("run", "p")
                events = store.events("run", limit=1000)
                scenarios.append({
                    "objective": objective, "instruction": instruction, "repeat": repeat,
                    "planning_contexts": coordinator.planning_contexts,
                    "rejections": [e["payload"] for e in events if e["kind"] == "subgoal_plan_rejected"],
                    "accepted": any(e["kind"] == "subgoal_plan_updated" for e in events),
                    "investigation": (getattr(coordinator, "investigation", None) or [None] * 3)[1:3],
                    "planner_rejected_issues": [i for i in issue_files(store) if i["type"] == "planner_rejected"],
                    "actions": sum(op.get("operation") == "perform_action" for op in coordinator.ops)})
                store.close()
    accepted = sum(s["accepted"] for s in scenarios)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    report = Path(os.environ.get("ASHAD_PLANNER_BENCH_DIR", ROOT / "tmp" / "planner-benchmark")) / f"{stamp}.json"
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text(json.dumps({"base": head(), "scenarios": scenarios, "accepted": accepted, "total": len(scenarios)},
                                 indent=1, default=str))
    rate = accepted / len(scenarios)
    print(f"planner acceptance {accepted}/{len(scenarios)} report {report}")
    assert all(s["actions"] == 0 for s in scenarios)
    assert not any("invalid subgoal planning JSON" in " ".join(map(str, s["investigation"])) for s in scenarios)
    target = float(os.environ.get("ASHAD_PLANNER_BENCH_TARGET", "0.8"))
    assert rate >= target, f"acceptance {rate:.2f} below {target}; report {report}"
