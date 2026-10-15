"""Get game controls: a parallel Claude Sonnet swarm over pinned source writes one versioned game controls file per project configuration."""
import asyncio
import hashlib
import json
import os
from pathlib import Path
import re
import time

from .prompts import SOURCE_NOTES_LIMIT, TUNING_MANUAL_LIMIT, TUNING_MINIMUM_BYTES, TUNING_PROMPT_VERSION
from .models import CLAUDE_CONTROLS_MODEL, CLAUDE_MODEL, provider_of
from .repository import Repository, sha256_file
from .projects import Projects, _identifier, _name
from .store import uid
from .workspace_environment import validate_build


SOURCE_BUDGET = 96000
SWARM_WORKERS = 4
CONTROLS_FORMAT = "ashad.game-controls"
CONTROLS_VERSION = 1
CONTROLS_FILE_LIMIT = 256 * 1024
CONTROLS_KEYS = frozenset({"format", "version", "configuration_id", "project_id", "build_id", "source_revision",
    "source_inventory_sha256", "source_files", "coverage", "generated_by", "created_at", "limitations", "controls"})
CLAUDE_REQUIRED = "Getting game controls uses Claude Opus. Connect Claude in Settings › Account; instances can still use Codex."
MODEL_NOT_ALLOWED = "Get game controls always uses Claude Opus; choose the testing model when you start an instance."
BUSY = "Game controls are already being gathered. Wait for them to finish."
CONFIGURATION_RETIRED = "This project configuration is retired. Get game controls again for this project, then start the instance with the new configuration."
SIGN_IN_WAIT = "Wait for Get game controls to finish before changing AI sign-in."
RETIRED_REASON = "Saved before game controls existed, and its saved instructions are invalid."
LIMITATIONS = ["AI-written controls knowledge from bounded source analysis; gameplay has not been verified.",
    "Get game controls prepares a reusable prompt, not model weights. Only currently advertised actions can execute."]
LEGACY_KEYS = ("model", "instructions", "instructions_sha256", "source_files", "source_inventory_sha256", "source_coverage", "limitations")
CONTROL = re.compile(r"Input\.|is_action_|func .*?(?:input|interact|pressed)|next_text|exit_menu")
CORE = ("project.godot", "action_executor.gd", "cutscene_manager.gd", "player_tablet.gd", "player.gd",
        "create_world.gd", "select_world_manager.gd", "main_menu.gd", "character_creation.gd",
        "text_object.gd", "player_inventory_manager.gd", "inventory_slot.gd", "state_manager.gd",
        "escape_back.gd", "npc_tablet.gd", "building_manager.gd", "fishing_manager.gd", "machine_window.gd",
        "context_registry.gd", "game_bridge.gd")


def source_evidence(build):
    repo = Repository(build)
    if not build.get("source_revision") or not repo.hashes:
        raise ValueError("Get game controls requires a build with a pinned source revision and source-hashes.json.")
    inventory_hash = sha256_file(repo.root.parent / "source-hashes.json")
    candidates, scanned = [], 0
    for relative in sorted(repo.hashes):
        path = Path(relative)
        if relative != "project.godot" and (path.suffix != ".gd" or not relative.startswith("scripts/")):
            continue
        if path.name == "dev_console.gd" or "/ashad/" in relative and path.name not in ("action_executor.gd", "context_registry.gd", "game_bridge.gd"):
            continue
        file = repo.resolve(relative)
        if not file.is_file():
            raise ValueError(f"Pinned source file is missing; rebuild before getting game controls: {relative}")
        if file.stat().st_size > 2 * 1024**2:
            raise ValueError(f"Source script exceeds the game controls read limit: {relative}")
        raw = file.read_bytes()
        if hashlib.sha256(raw).hexdigest() != repo.hashes[relative]:
            raise ValueError(f"Pinned source changed; rebuild before getting game controls: {relative}")
        lines = raw.decode("utf-8-sig").splitlines()
        scanned += 1
        hits = [i for i, line in enumerate(lines) if CONTROL.search(line)]
        relevant_path = any(word in relative.lower() for word in ("inventory", "craft", "building", "fishing", "dialogue", "main_menu"))
        if path.name not in CORE and not hits and not relevant_path:
            continue
        gameplay_path = relevant_path or any(word in relative for word in ("/world/objects/", "/world/npcs/", "/ui/npcs/"))
        priority = CORE.index(path.name) if path.name in CORE else len(CORE) if gameplay_path else len(CORE) + 1
        if relative == "project.godot":
            # Preserve the complete input map, omitting unrelated project configuration.
            start = lines.index("[input]") if "[input]" in lines else 0
            end = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("[")), len(lines))
        else:
            start, end = 0, len(lines)
        excerpt, used, chunk_start = [], 0, start + 1
        for i in range(start, end):
            line = lines[i]
            if relative == "project.godot" and "Object(InputEvent" in line:
                line = ", ".join(re.findall(r'InputEvent\w+|"(?:physical_keycode|keycode|button_index|unicode)":\d+', line))
            rendered = f"{i + 1}: {line}"
            size = len(json.dumps(rendered, ensure_ascii=False).encode("utf-8")) + 1
            if size > SOURCE_BUDGET // 2:
                raise ValueError(f"A source line exceeds the game controls context limit: {relative}:{i + 1}")
            if used + size > SOURCE_BUDGET // 2 and excerpt:
                candidates.append((priority, relative, {"path": relative, "sha256": repo.hashes[relative],
                    "total_lines": len(lines), "start": chunk_start, "end": i, "text": "\n".join(excerpt)}))
                excerpt, used, chunk_start = [], 0, i + 1
            excerpt.append(rendered)
            used += size
        if excerpt:
            candidates.append((priority, relative, {"path": relative, "sha256": repo.hashes[relative],
                "total_lines": len(lines), "start": chunk_start, "end": end, "text": "\n".join(excerpt)}))
    if not candidates:
        raise ValueError("No game controls source was found in the pinned build.")
    sources = [source for _, _, source in sorted(candidates, key=lambda item: (item[0], item[1], item[2]["start"]))]
    coverage = {"scanned_files": scanned, "included_files": len({s["path"] for s in sources}), "omitted_files": [],
                "scope": "Complete input map and relevant first-party controls, UI and interaction scripts; no debug console."}
    return {"build_id": build["id"], "source_revision": build["source_revision"], "sources": sources,
            "coverage": coverage}, inventory_hash


def controls_file(store, configuration_id):
    return store.root / "controls" / f"{configuration_id}.json"


def _write_file(store, record, artifact):
    data = json.dumps(artifact, ensure_ascii=False, sort_keys=True, indent=1).encode()
    if len(data) > CONTROLS_FILE_LIMIT:
        raise ValueError("Game controls file is too large; no game controls were saved.")
    path = controls_file(store, record["id"])
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_bytes(data)
    os.replace(temporary, path)
    record["controls_sha256"] = hashlib.sha256(data).hexdigest()


def remove_controls_file(store, configuration_id):
    try:
        controls_file(store, configuration_id).unlink(missing_ok=True)
    except OSError:
        return False
    return True


class GameControls:
    def __init__(self, store, ai):
        self.store, self.ai = store, ai
        self.lock = asyncio.Lock()
        self.progress = None  # The running Get game controls stage and batches, for GET /api/configurations/controls/<id>.

    def _validated_project_build(self, build_id, project_id):
        assigned = Projects(self.store).project_for_build(build_id, project_id)
        build = self.store.require("builds", build_id)
        try:
            validate_build(build)
        except (OSError, TypeError, KeyError, ValueError) as exc:
            raise ValueError(str(exc) or "Build is not validated for this project.") from None
        return assigned, build

    async def gather(self, build_id, *, request_id=None, name=None, project_id=None, configuration_id=None):
        if request_id is not None:
            _identifier(request_id, "Get game controls request ID")
        if configuration_id is not None:
            _identifier(configuration_id, "Project configuration ID")
        if name is not None:
            name = _name(name, "Configuration name")
        existing = self.store.get("configurations", configuration_id) if configuration_id else None
        if configuration_id:
            if existing is None:
                raise ValueError("Unknown project configuration.")
            if existing.get("status") == "retired":
                raise ValueError(CONFIGURATION_RETIRED)
            if project_id is not None and existing.get("project_id") not in (None, project_id):
                raise ValueError("Project configuration and build belong to different projects")
            if not build_id:
                build_id = existing["build_id"]
            name = existing.get("name") or name
        payload = {"build_id": build_id, "name": name, "project_id": project_id, "configuration_id": configuration_id}
        previous = self.store.command("tuning", request_id) if request_id else None
        if previous:
            if previous["request"] != payload:
                raise ValueError("Get game controls request ID already belongs to different inputs; Get controls again needs a new ID")
            return self.store.require("configurations", previous["configuration_id"])
        if self.lock.locked():
            raise ValueError(BUSY)
        async with self.lock:
            status = await self.ai.validate(CLAUDE_MODEL)
            if status.get("status") != "ready":
                raise ValueError(CLAUDE_REQUIRED)
            self.progress = {"stage": "reading_source", "batch_count": 0, "batches_done": 0, "batches_running": 0, "workers": 0, "started_at": time.time()}
            record = None
            previous_file = None
            try:
                assigned_project, build = self._validated_project_build(build_id, project_id)
                if configuration_id and existing and existing.get("project_id") not in (None, assigned_project):
                    raise ValueError("Project configuration and build belong to different projects")
                evidence, inventory_hash = await asyncio.to_thread(source_evidence, build)
                batches = self._batches(evidence["sources"])
                workers = min(SWARM_WORKERS, len(batches))
                if len(batches) == 1:
                    self.progress["stage"] = "writing"
                    controls = await self.ai.write_controls(evidence)
                else:
                    self.progress.update(stage="exploring", batch_count=len(batches), workers=workers)
                    notes = await self._swarm(build, batches)
                    self.progress["stage"] = "writing"
                    controls = await self.ai.write_controls({"build_id": build_id, "source_revision": build["source_revision"],
                        "source_notes": notes, "coverage": evidence["coverage"]})
                created = time.time()
                if configuration_id and existing:
                    record = {**existing, "name": name or existing.get("name") or f"{build.get('name', build_id)} controls",
                        "project_id": assigned_project, "build_id": build_id, "source_revision": build["source_revision"],
                        "status": "ready", "created_at": created, "controls_version": CONTROLS_VERSION}
                else:
                    record = {"id": uid("config_"), "name": name or f"{build.get('name', build_id)} controls",
                        "project_id": assigned_project, "build_id": build_id, "source_revision": build["source_revision"],
                        "status": "ready", "created_at": created, "controls_version": CONTROLS_VERSION}
                artifact = {"format": CONTROLS_FORMAT, "version": CONTROLS_VERSION, "configuration_id": record["id"],
                    "project_id": assigned_project, "build_id": build_id, "source_revision": build["source_revision"],
                    "source_inventory_sha256": inventory_hash,
                    "source_files": list({source["path"]: {k: source[k] for k in ("path", "sha256", "total_lines")}
                        for source in evidence["sources"]}.values()),
                    "coverage": {**evidence["coverage"], "source_batches": len(batches), "workers": workers},
                    "generated_by": {"method": "swarm", "provider": "claude", "model": CLAUDE_CONTROLS_MODEL, "prompt_version": TUNING_PROMPT_VERSION},
                    "created_at": record["created_at"], "limitations": list(LIMITATIONS), "controls": controls}
                if configuration_id:
                    try:
                        previous_file = controls_file(self.store, record["id"]).read_bytes()
                    except OSError:
                        previous_file = None
                _write_file(self.store, record, artifact)
                self._validate(record)
                with self.store.transaction():
                    self.store.put("configurations", record)
                    if request_id:
                        self.store.command("tuning", request_id, {"request": payload, "configuration_id": record["id"]})
                return record
            except BaseException:
                if record and not configuration_id:
                    remove_controls_file(self.store, record["id"])
                elif record and previous_file is not None:
                    controls_file(self.store, record["id"]).write_bytes(previous_file)  # Keep the saved controls usable after a failed regather.
                elif record:
                    remove_controls_file(self.store, record["id"])
                raise
            finally:
                self.progress = None

    @staticmethod
    def _batches(sources):
        batches, batch, used = [], [], 0
        for source in sources:
            size = len(json.dumps(source, ensure_ascii=False).encode("utf-8"))
            if used + size > SOURCE_BUDGET and batch:
                batches.append(batch)
                batch, used = [], 0
            batch.append(source)
            used += size
        batches.append(batch)
        if len(batches) > 12:
            raise ValueError("Relevant source exceeds 12 bounded source batches. Narrow the build source before getting game controls; no game controls were saved.")
        return batches

    async def _swarm(self, build, batches):
        notes = [None] * len(batches)
        gate = asyncio.Semaphore(SWARM_WORKERS)

        async def worker(index, batch):
            async with gate:
                self.progress["batches_running"] += 1
                try:
                    batch_evidence = {"build_id": build["id"], "source_revision": build["source_revision"],
                        "batch": index + 1, "batch_count": len(batches), "sources": batch}
                    notes_key = hashlib.sha256(json.dumps({"evidence": batch_evidence, "model": CLAUDE_CONTROLS_MODEL,
                        "prompt_version": TUNING_PROMPT_VERSION}, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
                    saved = self.store.get("tuning_notes", notes_key, {})
                    note = saved.get("instructions")
                    if not isinstance(note, str) or len(note.strip().encode("utf-8")) < TUNING_MINIMUM_BYTES or len(note.encode("utf-8")) > SOURCE_NOTES_LIMIT:
                        note = await self.ai.write_controls(batch_evidence, stage="source_notes")
                        self.store.put("tuning_notes", {"id": notes_key, "build_id": build["id"], "model": CLAUDE_CONTROLS_MODEL,
                            "prompt_version": TUNING_PROMPT_VERSION, "instructions": note})
                    notes[index] = note
                    self.progress["batches_done"] += 1
                finally:
                    self.progress["batches_running"] -= 1

        try:
            async with asyncio.TaskGroup() as group:
                for index, batch in enumerate(batches):
                    group.create_task(worker(index, batch))
        except* Exception as failures:
            raise failures.exceptions[0]
        return notes

    def validated(self, configuration_id):
        record = self.store.get("configurations", configuration_id)
        if record is None:
            raise ValueError("Unknown project configuration.")
        if record.get("status") != "ready":
            raise ValueError(CONFIGURATION_RETIRED)
        if record.get("controls_version") != CONTROLS_VERSION:
            raise ValueError("Game controls file version is not supported by this ASHAD. Get game controls again.")
        return record, self._validate(record)

    def _validate(self, record):
        again = "Get game controls again."
        path = controls_file(self.store, record["id"])
        try:
            if path.stat().st_size > CONTROLS_FILE_LIMIT:
                raise ValueError(f"Game controls file is too large. {again}")
            data = path.read_bytes()
        except OSError:
            raise ValueError(f"Game controls file is missing. {again}") from None
        if hashlib.sha256(data).hexdigest() != record.get("controls_sha256"):
            raise ValueError(f"Game controls file changed. {again}")
        try:
            artifact = json.loads(data)
        except ValueError:
            raise ValueError(f"Game controls file is not valid JSON. {again}") from None
        if not isinstance(artifact, dict) or set(artifact) != CONTROLS_KEYS:
            raise ValueError(f"Game controls file has unexpected contents. {again}")
        if artifact["format"] != CONTROLS_FORMAT or artifact["version"] != CONTROLS_VERSION:
            raise ValueError(f"Game controls file format is not supported. {again}")
        controls = artifact["controls"]
        if not isinstance(controls, str) or not TUNING_MINIMUM_BYTES <= len(controls.encode("utf-8")) <= TUNING_MANUAL_LIMIT:
            raise ValueError(f"Game controls text is invalid. {again}")
        if (artifact["build_id"], artifact["source_revision"], artifact["configuration_id"]) != (record["build_id"], record.get("source_revision"), record["id"]):
            raise ValueError(f"Game controls file belongs to a different configuration. {again}")
        build = self.store.get("builds", record["build_id"])
        if not build:
            raise ValueError(f"Pinned build {record['build_id']} is no longer registered. Get controls again on a current build to continue.")
        if record.get("source_revision") != build.get("source_revision"):
            raise ValueError("Game controls no longer match this build. Get controls again on a current build to continue.")
        repo = Repository(build)
        try:
            validate_build(build)
        except (OSError, TypeError, KeyError, ValueError) as exc:
            raise ValueError(str(exc) or "Pinned build is no longer valid. Get controls again on a current build to continue.") from None
        if not repo.hashes or sha256_file(repo.root.parent / "source-hashes.json") != artifact["source_inventory_sha256"]:
            raise ValueError("Pinned source inventory changed. Get controls again on a current build to continue.")
        if not artifact["source_files"]:
            raise ValueError(f"Game controls have no verified source references. {again}")
        for source in artifact["source_files"]:
            file = repo.resolve(source["path"])
            if source["sha256"] != repo.hashes.get(source["path"]) or not file.is_file() or sha256_file(file) != source["sha256"]:
                raise ValueError(f"Pinned source changed: {source['path']}. Get controls again on a current build to continue.")
        return artifact


def migrate_configuration_names(store):
    """One-way, idempotent startup rename of stored agent records and command keys to configuration names."""
    with store.transaction() as db:
        clash = db.execute("SELECT id FROM records WHERE kind='agents' AND id IN (SELECT id FROM records WHERE kind='configurations')").fetchone()
        if clash:
            raise RuntimeError(f"Stored project configuration {clash[0]} exists under both 'agents' and 'configurations'; ASHAD stopped without changing data.")
        db.execute("UPDATE records SET kind='configurations' WHERE kind='agents'")
        db.execute("UPDATE commands SET result=json_remove(json_set(result,'$.configuration_id',json_extract(result,'$.agent_id')),'$.agent_id') "
                   "WHERE scope='tuning' AND json_type(result,'$.agent_id') IS NOT NULL")
        db.execute("UPDATE commands SET result=json_remove(json_set(result,'$.request.configuration_id',json_extract(result,'$.request.agent_id')),'$.request.agent_id') "
                   "WHERE scope='instance' AND json_type(result,'$.request.agent_id') IS NOT NULL")


def run_configuration(run):
    """Return a stored run's (configuration id, configuration snapshot), reading legacy agent keys too."""
    plan = run.get("plan") or {}
    return (run.get("configuration_id") or run.get("agent_id") or plan.get("configuration_id") or plan.get("agent_id"),
            run.get("configuration") or run.get("agent_profile"))


def migrate_tuned_configurations(store):
    """One-way, idempotent startup conversion of tuned agent configurations into v1 game controls files."""
    for record in store.list("configurations"):
        if "controls_version" in record:
            continue
        instructions = record.get("instructions")
        valid = (record.get("status") == "ready" and isinstance(instructions, str)
                 and TUNING_MINIMUM_BYTES <= len(instructions.encode("utf-8")) <= TUNING_MANUAL_LIMIT
                 and hashlib.sha256(instructions.encode("utf-8")).hexdigest() == record.get("instructions_sha256"))
        converted = {k: v for k, v in record.items() if k not in LEGACY_KEYS}
        converted["controls_version"] = CONTROLS_VERSION
        if valid:
            model = record.get("model")
            artifact = {"format": CONTROLS_FORMAT, "version": CONTROLS_VERSION, "configuration_id": record["id"],
                "project_id": record.get("project_id"), "build_id": record["build_id"], "source_revision": record.get("source_revision"),
                "source_inventory_sha256": record.get("source_inventory_sha256"), "source_files": record.get("source_files"),
                "coverage": record.get("source_coverage"),
                "generated_by": {"method": "legacy_tuning", "provider": provider_of(model), "model": model, "prompt_version": None},
                "created_at": record.get("created_at"), "limitations": record.get("limitations"), "controls": instructions}
            _write_file(store, converted, artifact)
        else:
            converted.update(status="retired", retired_reason=RETIRED_REASON)
        with store.transaction():
            store.put("configurations", converted)
    with store.transaction() as db:
        db.execute("DELETE FROM records WHERE kind='tuning_notes' AND json_extract(data,'$.model') != ?", (CLAUDE_CONTROLS_MODEL,))
