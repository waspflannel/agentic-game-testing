"""Reusable source-grounded controls manuals, pinned to one game build."""
import asyncio
import hashlib
import json
from pathlib import Path
import re
import time

from .codex_backend import TUNING_MANUAL_LIMIT, TUNING_PROMPT_VERSION
from .models import MODEL, SUPPORTED_MODELS
from .repository import Repository, sha256_file
from .projects import Projects, _identifier, _name
from .store import uid


SOURCE_BUDGET = 96000
CONTROL = re.compile(r"Input\.|is_action_|func .*?(?:input|interact|pressed)|next_text|exit_menu")
CORE = ("project.godot", "action_executor.gd", "cutscene_manager.gd", "player_tablet.gd", "player.gd",
        "create_world.gd", "select_world_manager.gd", "main_menu.gd", "character_creation.gd",
        "text_object.gd", "player_inventory_manager.gd", "inventory_slot.gd", "state_manager.gd",
        "escape_back.gd", "npc_tablet.gd", "building_manager.gd", "fishing_manager.gd", "machine_window.gd")


def source_evidence(build):
    repo = Repository(build)
    if not build.get("source_revision") or not repo.hashes:
        raise ValueError("Tune agent requires a build with a pinned source revision and source-hashes.json.")
    inventory_hash = sha256_file(repo.root.parent / "source-hashes.json")
    candidates, scanned = [], 0
    for relative in sorted(repo.hashes):
        path = Path(relative)
        if relative != "project.godot" and (path.suffix != ".gd" or not relative.startswith("scripts/")):
            continue
        if path.name == "dev_console.gd" or "/ashad/" in relative and path.name != "action_executor.gd":
            continue
        file = repo.resolve(relative)
        if not file.is_file():
            raise ValueError(f"Pinned source file is missing; rebuild before tuning: {relative}")
        if file.stat().st_size > 2 * 1024**2:
            raise ValueError(f"Source script exceeds the tuning read limit: {relative}")
        raw = file.read_bytes()
        if hashlib.sha256(raw).hexdigest() != repo.hashes[relative]:
            raise ValueError(f"Pinned source changed; rebuild before tuning: {relative}")
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
                raise ValueError(f"A source line exceeds the tuning context limit: {relative}:{i + 1}")
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


class AgentProfiles:
    def __init__(self, store, ai):
        self.store, self.ai = store, ai
        self.lock = asyncio.Lock()

    async def tune(self, build_id, model=MODEL, *, request_id=None, name=None, project_id=None):
        if request_id is not None:
            _identifier(request_id, "Tuning request ID")
        if name is not None:
            name = _name(name, "Agent configuration name")
        payload = {"build_id": build_id, "model": model, "name": name, "project_id": project_id}
        if model not in SUPPORTED_MODELS:
            raise ValueError("Select a supported Codex model.")
        previous = self.store.command("tuning", request_id) if request_id else None
        if previous:
            if previous["request"] != payload:
                raise ValueError("Tuning request ID already belongs to different inputs; explicit retune needs a new ID")
            return self.store.require("agents", previous["agent_id"])
        if self.lock.locked():
            raise ValueError("An agent is already being tuned. Wait for it to finish before tuning another.")
        async with self.lock:
            assigned_project = Projects(self.store).project_for_build(build_id, project_id)
            build = self.store.require("builds", build_id)
            evidence, inventory_hash = await asyncio.to_thread(source_evidence, build)
            batches, batch, used = [], [], 0
            for source in evidence["sources"]:
                size = len(json.dumps(source, ensure_ascii=False).encode("utf-8"))
                if used + size > SOURCE_BUDGET and batch:
                    batches.append(batch)
                    batch, used = [], 0
                batch.append(source)
                used += size
            batches.append(batch)
            if len(batches) > 12:
                raise ValueError("Relevant source exceeds 12 bounded tuning batches. Narrow the build source before tuning; no partial agent was saved.")
            evidence["coverage"]["source_batches"] = len(batches)
            if len(batches) == 1:
                instructions = await self.ai.tune_agent(evidence, model=model)
            else:
                notes = []
                for index, batch in enumerate(batches):
                    batch_evidence = {"build_id": build_id, "source_revision": build["source_revision"],
                        "batch": index + 1, "batch_count": len(batches), "sources": batch}
                    notes_key = hashlib.sha256(json.dumps({"evidence": batch_evidence, "model": model,
                        "prompt_version": TUNING_PROMPT_VERSION}, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
                    saved = self.store.get("tuning_notes", notes_key, {})
                    note = saved.get("instructions")
                    if not isinstance(note, str) or len(note.strip().encode("utf-8")) < 200 or len(note.encode("utf-8")) > 8000:
                        note = await self.ai.tune_agent(batch_evidence, model=model, stage="source_notes")
                        self.store.put("tuning_notes", {"id": notes_key, "build_id": build_id, "model": model,
                            "prompt_version": TUNING_PROMPT_VERSION, "instructions": note})
                    notes.append(note)
                instructions = await self.ai.tune_agent({"build_id": build_id, "source_revision": build["source_revision"],
                    "source_notes": notes, "coverage": evidence["coverage"]}, model=model)
            profile = {"id": uid("agent_"), "name": name or f"{build.get('name', build_id)} agent",
                "project_id": assigned_project, "build_id": build_id, "source_revision": build["source_revision"], "model": model,
                "instructions": instructions, "instructions_sha256": hashlib.sha256(instructions.encode("utf-8")).hexdigest(),
                "source_files": list({source["path"]: {k: source[k] for k in ("path", "sha256", "total_lines")}
                    for source in evidence["sources"]}.values()),
                "source_inventory_sha256": inventory_hash, "source_coverage": evidence["coverage"],
                "limitations": ["AI-written controls knowledge from bounded source analysis; gameplay has not been verified.",
                    "Tuning prepares a reusable prompt, not model weights. Only currently advertised actions can execute."],
                "created_at": time.time(), "status": "ready"}
            self._validate(profile)
            with self.store.transaction():
                self.store.put("agents", profile)
                if request_id:
                    self.store.command("tuning", request_id, {"request": payload, "agent_id": profile["id"]})
            return profile

    def validated(self, agent_id):
        profile = self.store.require("agents", agent_id)
        self._validate(profile)
        return profile

    def _validate(self, profile):
        build = self.store.require("builds", profile["build_id"])
        if profile.get("status") != "ready" or profile.get("source_revision") != build.get("source_revision"):
            raise ValueError("Agent knowledge no longer matches this build. Tune a new agent before launching.")
        instructions = profile.get("instructions", "")
        if not isinstance(instructions, str) or not 200 <= len(instructions.encode("utf-8")) <= TUNING_MANUAL_LIMIT or hashlib.sha256(instructions.encode("utf-8")).hexdigest() != profile.get("instructions_sha256"):
            raise ValueError("Agent controls manual is invalid or changed. Tune a new agent before launching.")
        repo = Repository(build)
        if not repo.hashes or sha256_file(repo.root.parent / "source-hashes.json") != profile.get("source_inventory_sha256"):
            raise ValueError("Pinned source inventory changed. Tune a new agent for the rebuilt game.")
        if not profile.get("source_files"):
            raise ValueError("Agent has no verified source references. Tune a new agent before launching.")
        for source in profile["source_files"]:
            file = repo.resolve(source["path"])
            if source["sha256"] != repo.hashes.get(source["path"]) or not file.is_file() or sha256_file(file) != source["sha256"]:
                raise ValueError(f"Pinned source changed: {source['path']}. Rebuild and tune a new agent.")
