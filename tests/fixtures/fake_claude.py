"""A local subprocess fixture: never calls a provider or reads real Claude credentials."""
import json
import os
import sys
import time

# The backend gives the child a scrubbed environment, so tests pass the scenario as a leading
# --scenario=NAME argument (stripped before recording); FAKE_CLAUDE_SCENARIO is the fallback.
argv = sys.argv[1:]
scenario = os.environ.get("FAKE_CLAUDE_SCENARIO") or "success"
if argv and argv[0].startswith("--scenario="):
    scenario = argv.pop(0).split("=", 1)[1]
if "--version" in argv:
    print("2.1.200 (Claude Code)" if scenario == "old-version" else "2.1.280 (Claude Code)")
    sys.exit(0)
stdin = sys.stdin.buffer.read().decode("utf-8")
env = {name: os.environ.get(name) for name in (
    "HOME", "USERPROFILE", "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_OAUTH_TOKEN", "CLAUDE_CODE_DISABLE_FAST_MODE",
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CODEX_HOME", "CLAUDE_OAUTH_TEST_SECRET")}
with open(os.path.join(os.environ["CLAUDE_CONFIG_DIR"], "invocation.json"), "w", encoding="utf-8") as handle:
    json.dump({"argv": argv, "cwd": os.getcwd(), "stdin": stdin, "env": env}, handle)


def error(status):
    return json.dumps({"is_error": True, "api_error_status": status, "result": "raw-provider-secret", "terminal_reason": "error"})


def success(**extra):
    return json.dumps({"type": "result", "subtype": "success", "is_error": False, "result": "", **extra})


if scenario == "unauthorized":
    print(error(401), end="")
    sys.exit(1)
elif scenario == "rate-limited":
    print(error(429), end="")
    sys.exit(1)
elif scenario == "server-error":
    print(error(500), end="")
    sys.exit(1)
elif scenario == "list-output":
    print(success(structured_output=["a"]), end="")
elif scenario == "missing-output":
    print(success(), end="")
elif scenario == "oversized":
    # Valid success JSON, so only the output byte cap can reject it.
    print(success(structured_output={"answer": "x" * (9 * 1024 * 1024)}), end="")
elif scenario == "hang":
    time.sleep(10)
elif scenario == "crash":
    sys.stderr.write("raw-provider-secret")
    sys.exit(3)
elif scenario == "nonzero-exit":
    print(success(structured_output={"answer": "ok"}), end="")
    sys.exit(2)
elif scenario == "choice":
    print(success(structured_output={"action": "a"}), end="")
else:
    print(success(structured_output={"answer": "ok"}), end="")
