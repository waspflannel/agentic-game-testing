"""Saves pair the game's own save tree with the bot's state. They live in the run dir and are deleted with the run."""
import base64
import copy
import hashlib
import shutil
import time

from .store import uid
from .structured import OWN_ACTION_KINDS, observed_entity

RISKY_SAVE_MIN_SECONDS = 15
KEEP_RISKY = 20
KEEP_MANUAL = 10
MAX_PINNED = 30
RESTORED_ACTIONS = 20
MAX_ATTEMPTED = 200
FAILED = {"rejected", "failed", "expired", "cancelled", "unknown"}


def _newest_first(records):
    return sorted(records, key=lambda r: r["created_at"], reverse=True)


class Saves:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.store = coordinator.store

    def records(self, run_id):
        return _newest_first(self.store.records_for_run("saves", run_id))

    def archive_path(self, run_id, save_id):
        return self.store.run_dir(run_id) / "saves" / save_id / "game.zip"

    async def capture(self, run_id, player_id, trigger, detail):
        try:
            run, ps = self.coordinator.structured.binding(run_id, player_id)
            save_id = uid("save_")
            result = await self.coordinator.request(ps["worker_id"], "POST", "/saves/capture",
                                                    {"epoch": ps["epoch"], "save_id": save_id})
            if result["game_save"] != "none":
                blob = base64.b64decode(result["archive_base64"])
                if hashlib.sha256(blob).hexdigest() != result["sha256"]:
                    raise ValueError("Save archive checksum mismatch")
                path = self.archive_path(run_id, save_id)
                path.parent.mkdir(parents=True, exist_ok=True)
                temp = path.with_suffix(".tmp")
                temp.write_bytes(blob)
                temp.replace(path)
            events = self.coordinator.structured._recent_events(run_id)
            recent_actions = [e for e in events if e["kind"] in OWN_ACTION_KINDS
                              and e["payload"].get("player_id") == player_id][-20:]
            record = {"id": save_id, "run_id": run_id, "project_id": run.get("project_id"), "player_id": player_id,
                      "created_at": time.time(), "trigger": trigger, "trigger_detail": detail,
                      "ledger_seq": self.store.count_events(run_id),
                      "observation_signature": ps.get("observation_signature"), "pinned": trigger == "issue",
                      "game": {"status": result["game_save"], "detail": result["detail"],
                               "sha256": result["sha256"], "bytes": result["bytes"]},
                      "state": {"model": run["model"], "plan": copy.deepcopy(run["approved_plan"]),
                                "configuration_id": run.get("configuration_id"),
                                "build_id": run["plan"]["build_id"], "identity": ps["identity"],
                                "transport": run["plan"]["transport"]["mode"],
                                "subgoal_plan": copy.deepcopy(run.get("subgoal_plan")),
                                "agent_memory": ps.get("agent_memory", ""),
                                "attempted_actions": list(ps.get("attempted_actions", [])),
                                "recent_actions": recent_actions}}
            self.store.put("saves", record)
            self.store.event(run_id, "save_created", {"player_id": player_id, "save_id": save_id,
                                                      "trigger": trigger, "game_status": result["game_save"]})
            self.prune(run_id)
            return record
        except Exception as exc:
            # Only the type: worker errors can carry credentials.
            self.store.event(run_id, "save_failed", {"player_id": player_id, "trigger": trigger,
                                                     "error": type(exc).__name__})
            return None

    async def before_action(self, run_id, player_id, args, observation):
        run = self.store.require("runs", run_id)
        ps = run["players"][player_id]
        target_kind = observed_entity(observation, args.get("target_id", "")).get("kind") or "none"
        pair = f"{args['verb']}:{target_kind}"
        attempted = ps.get("attempted_actions", [])
        if pair not in attempted:
            why = "first_attempt"
        else:
            results = [e for e in self.coordinator.structured._recent_events(run_id)
                       if e["kind"] == "action_result" and e["payload"].get("player_id") == player_id]
            failed = bool(results) and (results[-1]["payload"].get("result") or {}).get("status") in FAILED
            why = "after_failure" if failed else None
        if pair not in attempted:
            ps["attempted_actions"] = (attempted + [pair])[-MAX_ATTEMPTED:]
            self.store.put("runs", run)
        if why is None:
            return
        saves = self.records(run_id)
        if saves and saves[0]["observation_signature"] == ps.get("observation_signature"):
            self.store.event(run_id, "save_skipped", {"player_id": player_id, "reason": "unchanged"})
            return
        risky = [s for s in saves if s["trigger"] == "risky_action"]
        if risky and time.time() - risky[0]["created_at"] < RISKY_SAVE_MIN_SECONDS:
            self.store.event(run_id, "save_skipped", {"player_id": player_id, "reason": "throttled"})
            return
        await self.capture(run_id, player_id, "risky_action",
                           {"why": why, "verb": args["verb"], "target_id": args.get("target_id", ""),
                            "target_kind": target_kind})

    def latest(self, run_id):
        records = self.records(run_id)
        return records[0] if records else None

    def pin(self, save_id):
        record = self.store.get("saves", save_id)
        if record:
            record["pinned"] = True
            self.store.put("saves", record)

    def pinned_full(self, run_id):
        return sum(bool(r["pinned"]) for r in self.records(run_id)) >= MAX_PINNED

    def prune(self, run_id):
        records = self.records(run_id)
        for trigger, keep in (("risky_action", KEEP_RISKY), ("manual", KEEP_MANUAL)):
            unpinned = [r for r in records if r["trigger"] == trigger and not r["pinned"]]
            for r in unpinned[keep:]:
                self.store.delete("saves", r["id"])
                shutil.rmtree(self.archive_path(run_id, r["id"]).parent, ignore_errors=True)


def summary(record):
    return {**{k: v for k, v in record.items() if k != "state"}, "model": record["state"]["model"]}


def restore_state(run, save):
    state = save["state"]
    if state["subgoal_plan"]:
        run["subgoal_plan"] = {**copy.deepcopy(state["subgoal_plan"]), "run_id": run["id"]}
    (ps,) = run["players"].values()
    ps["agent_memory"] = state["agent_memory"]
    ps["attempted_actions"] = list(state["attempted_actions"])
    ps["restored_actions"] = state["recent_actions"][-RESTORED_ACTIONS:]
    run["loaded_save"] = {"save_id": save["id"], "source_run_id": save["run_id"],
                          "created_at": save["created_at"], "game_save": save["game"]["status"]}
    run["recovery_instruction"] = ("This instance continues from a save. If the game menu offers the saved world, "
                                   "load it instead of creating a new one. Observe before acting; the earliest "
                                   "recent actions happened before the save.")
