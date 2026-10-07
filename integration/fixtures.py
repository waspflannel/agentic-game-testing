"""Read and validate Agronauts v30 save packages without mutating saves.

Host data contains the world and every saved character. Each joining worker also
retains its own local_player_id.txt/settings. These functions never rename a
character file or convert between transport identities implicitly.
"""
from __future__ import annotations

import hashlib
import io
import json
import re
import zipfile
from pathlib import Path, PurePosixPath


def local_identity_key(identity: str) -> str:
    """Godot 4 String.hash: 32-bit DJB2 over Unicode codepoints."""
    value = 5381
    for character in identity:
        value = (value * 33 + ord(character)) & 0xFFFFFFFF
    return str(value)


def character_key(identity: str, transport: str) -> str:
    if transport == "steam":
        if not re.fullmatch(r"[0-9]{15,20}", identity):
            raise ValueError("A Steam character requires its actual SteamID64")
        return identity
    if transport != "enet":
        raise ValueError("Transport must be enet or steam")
    if not identity:
        raise ValueError("Local identity is required")
    return local_identity_key(identity)


def read_json_lines(path: Path) -> list[dict]:
    records = []
    with path.open(encoding="utf-8-sig") as stream:
        for line_number, line in enumerate(stream, 1):
            if line.strip():
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError(f"{path.name}:{line_number} is not a save record")
                records.append(record)
    return records


def inspect_save(data_root: str | Path, world_id: str | None = None) -> dict:
    root = Path(data_root).resolve()
    worlds = read_json_lines(root / "worlds.save")
    if world_id is None:
        if len(worlds) != 1:
            raise ValueError("Select a world_id explicitly when a save tree contains multiple worlds")
        world_id = str(worlds[0]["world_id"])
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", world_id):
        raise ValueError("Invalid world ID in save")
    metadata = next((world for world in worlds if str(world.get("world_id")) == world_id), None)
    if metadata is None:
        raise ValueError("World ID is absent from worlds.save")
    save_root = root / "worlds" / world_id
    records = read_json_lines(save_root / "savegame.save")
    quests = next((row for row in records if str(row.get("path")) == "/root/QuestsManager"), {})
    demo = next((row for row in records if str(row.get("path", "")).endswith("/Demo")), {})
    character_files = {}
    for path in sorted((save_root / "players").glob("*.save")):
        if not path.stem.isdigit():
            raise ValueError("Non-numeric character identity key")
        character = read_json_lines(path)
        if not character:
            raise ValueError(f"Empty character record: {path.name}")
        character_files[path.stem] = {"path": path.relative_to(root).as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    hashes = {path.relative_to(root).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest() for path in sorted(save_root.rglob("*")) if path.is_file()}
    return {"world_id": world_id, "world_name": metadata.get("world_name"), "save_version": metadata.get("world_version", 0), "starting_milestones": list(quests.get("completed_quests", [])), "unlocked_recipes": list(quests.get("unlocked_recipes", [])), "demo_state": demo, "character_files": character_files, "files": hashes, "local_identity": (root / "local_player_id.txt").read_text().strip() if (root / "local_player_id.txt").is_file() else None}


def validate_character_mapping(save: dict, identities: dict[str, str], transport: str, character_policy: str = "restore") -> dict:
    if character_policy not in {"fresh", "restore"}:
        raise ValueError("Character policy must be fresh or restore")
    if save["save_version"] != 30:
        raise ValueError(f"Selected build accepts only save version 30, received {save['save_version']}")
    if len(identities) != len(set(identities.values())):
        raise ValueError("Player identities must be unique")
    mapping = {}
    for player_id, identity in identities.items():
        key = character_key(str(identity), transport)
        if character_policy == "restore" and key not in save["character_files"]:
            raise ValueError(f"{player_id}: character {key}.save is missing; changed identities require explicit validated conversion")
        mapping[player_id] = {"identity": str(identity), "identity_key": key, "character_file": save["character_files"].get(key), "policy": character_policy}
    return mapping


def _archive_files(blob: bytes) -> dict[str, bytes]:
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        entries = archive.infolist()
        if sum(entry.file_size for entry in entries) > 1024**3:
            raise ValueError("Unpacked fixture exceeds 1 GiB")
        result = {}
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if path.is_absolute() or ".." in path.parts or path.as_posix() != entry.filename.rstrip("/") or "\\" in entry.orig_filename or "\x00" in entry.orig_filename or ":" in entry.filename or (entry.external_attr >> 16) & 0o170000 == 0o120000:
                raise ValueError("Unsafe fixture archive path")
            if not entry.is_dir():
                if entry.filename in result:
                    raise ValueError("Duplicate fixture archive path")
                result[entry.filename] = archive.read(entry)
        return result


def _pack(files: dict[str, bytes], manifest: dict) -> bytes:
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(files):
            if name != "ashad-fixture.json":
                archive.writestr(name, files[name])
        archive.writestr("ashad-fixture.json", json.dumps(manifest, ensure_ascii=False))
    return stream.getvalue()


def _validate_manifest(files: dict[str, bytes], manifest: dict) -> None:
    if "ashad-fixture.json" not in files:
        raise ValueError("Fixture is missing its embedded manifest")
    embedded = json.loads(files["ashad-fixture.json"])
    for field in ("world_id", "build_revision", "save_version", "transport", "identity_mapping", "starting_milestones"):
        if embedded.get(field) != manifest.get(field):
            raise ValueError(f"Embedded fixture manifest differs: {field}")
    world_id = str(manifest.get("world_id", ""))
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", world_id):
        raise ValueError("Invalid world ID")
    world_path = f"worlds/{world_id}/savegame.save"
    if world_path not in files:
        return  # Joining clients keep their identity/settings; host owns the world.
    metadata = [json.loads(line) for line in files.get("worlds.save", b"").decode("utf-8-sig").splitlines() if line.strip()]
    matches = [row for row in metadata if row.get("world_id") == world_id]
    if len(matches) != 1 or matches[0].get("world_version") != manifest.get("save_version"):
        raise ValueError("Saved world metadata differs from fixture manifest")
    records = [json.loads(line) for line in files[world_path].decode("utf-8-sig").splitlines() if line.strip()]
    quests = [row for row in records if row.get("path") == "/root/QuestsManager"]
    if len(quests) != 1 or sorted(quests[0].get("completed_quests", [])) != sorted(manifest.get("starting_milestones", [])):
        raise ValueError("Saved world milestones differ from fixture manifest")


def prepare_archive(blob: bytes, manifest: dict, player_mapping: dict[str, str], character_policy: str, target_transport: str) -> tuple[bytes, dict]:
    """Stage a fresh-character or same-identity restore into a NEW ZIP only.

    player_mapping maps target player IDs to their explicit persistent identities.
    A host archive must contain all requested restored character records. A client
    archive can contain settings/identity alone; the host package validates its
    character records. This helper is called before game launch, never mid-play.
    """
    if character_policy not in {"fresh", "restore"}:
        raise ValueError("Character policy must be fresh or restore")
    if manifest.get("save_version") != 30:
        raise ValueError("Selected converter supports only save version 30")
    if target_transport not in {"enet", "steam"} or manifest.get("transport") != target_transport:
        raise ValueError("Cross-transport setup requires an explicit converted package")
    mapping = {str(player): str(identity) for player, identity in player_mapping.items()}
    if not mapping or len(mapping) != len(set(mapping.values())):
        raise ValueError("Unique explicit player identities are required")
    keys = {player: character_key(identity, target_transport) for player, identity in mapping.items()}
    if len(keys) != len(set(keys.values())):
        raise ValueError("Player identities collide in Godot's character keys")
    files = _archive_files(blob)
    _validate_manifest(files, manifest)
    world_id = str(manifest.get("world_id", ""))
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", world_id):
        raise ValueError("Invalid world ID")
    character_prefix = f"worlds/{world_id}/players/"
    host_world = f"worlds/{world_id}/savegame.save" in files
    operations = []
    if character_policy == "restore":
        original = set(map(str, manifest.get("identity_mapping", {}).values()))
        for player, identity in mapping.items():
            if identity not in original:
                raise ValueError(f"{player}: changed identity requires an explicit converted package")
            if host_world and character_prefix + keys[player] + ".save" not in files:
                raise ValueError(f"{player}: saved character file is missing")
    else:
        # Fresh character policy does not reset any world/quest/recipe progression.
        for name in list(files):
            if re.fullmatch(r"worlds/[^/]+/players/[^/]+\.save", name):
                del files[name]
                operations.append({"operation": "omit_character", "path": name})
        world_path = f"worlds/{world_id}/savegame.save"
        if host_world:
            lines = []
            for line in files[world_path].splitlines(keepends=True):
                if line.strip():
                    record = json.loads(line)
                    if record.get("path") == "/root/Game/CutsceneLayer/CutsceneManager" and "_played_cutscene" in record:
                        played = json.loads(record["_played_cutscene"])
                        if not isinstance(played, dict):
                            raise ValueError("Invalid v30 character cutscene history")
                        removed = sorted(set(played).intersection(keys.values()))
                        if removed:
                            for key in removed:
                                del played[key]
                            record["_played_cutscene"] = json.dumps(played, separators=(",", ":"))
                            ending = b"\r\n" if line.endswith(b"\r\n") else b"\n" if line.endswith(b"\n") else b""
                            prefix = b"\xef\xbb\xbf" if line.startswith(b"\xef\xbb\xbf") else b""
                            line = prefix + json.dumps(record, separators=(",", ":")).encode() + ending
                            operations.append({"operation": "omit_fresh_character_cutscene_history", "path": world_path, "identity_keys": removed})
                lines.append(line)
            files[world_path] = b"".join(lines)
    if "local_player_id.txt" in files:
        # The assigned worker writes its validated identity during pre-game boot.
        # This avoids importing the host's local UUID into a joining environment.
        del files["local_player_id.txt"]
        operations.append({"operation": "omit_source_local_identity", "reason": "Worker persists the explicitly mapped target identity before gameplay"})
    result = {**manifest, "identity_mapping": mapping, "character_policy": character_policy, "transport": target_transport}
    result["setup_audit"] = list(manifest.get("setup_audit", [])) + [{"operation": "prepare_archive", "source_sha256": hashlib.sha256(blob).hexdigest(), "character_policy": character_policy, "target_identity_keys": keys, "world_progress_preserved": True, "changes": operations}]
    return _pack(files, result), result


def convert_archive(blob: bytes, manifest: dict, identity_mapping: dict[str, str], target_transport: str) -> tuple[bytes, dict]:
    """Explicit offline v30 conversion; caller supplies every old→new identity.

    Only known persistent identity locations are translated: character filenames,
    the CutsceneManager's `_played_cutscene` map, and local identity setup. World
    progress, inventories, quantities and positions are preserved byte-for-byte
    except JSON serialization of the cutscene record. No equivalence is inferred.
    """
    if manifest.get("save_version") != 30:
        raise ValueError("Conversion supports only inspected save version 30")
    source_transport = manifest.get("transport")
    source_ids = set(map(str, manifest.get("identity_mapping", {}).values()))
    if set(identity_mapping) != source_ids:
        raise ValueError("Provide an explicit mapping for every source player identity")
    if len(set(identity_mapping.values())) != len(identity_mapping):
        raise ValueError("Converted player identities must be unique")
    keys = {character_key(old, source_transport): character_key(new, target_transport) for old, new in identity_mapping.items()}
    if len(keys) != len(identity_mapping) or len(keys.values()) != len(set(keys.values())):
        raise ValueError("Converted identities collide in Godot's character keys")
    files = _archive_files(blob)
    _validate_manifest(files, manifest)
    world_prefix = f"worlds/{manifest['world_id']}/"
    if world_prefix + "savegame.save" in files:
        for key in keys:
            if world_prefix + "players/" + key + ".save" not in files:
                raise ValueError("A source character file is missing")
    changes = []
    converted = {}
    for name, payload in files.items():
        match = re.fullmatch(r"(worlds/[^/]+/players/)(\d+)(\.save)", name)
        if match and match.group(2) in keys:
            destination = match.group(1)+keys[match.group(2)]+match.group(3)
            if destination in converted:
                raise ValueError("Character destination collision")
            converted[destination] = payload
            changes.append({"operation": "map_character_key", "source": name, "destination": destination})
        elif name.endswith("/savegame.save"):
            lines = []
            for line in payload.decode("utf-8-sig").splitlines(keepends=True):
                if not line.strip():
                    lines.append(line)
                    continue
                record = json.loads(line)
                if "_played_cutscene" in record:
                    old = json.loads(record["_played_cutscene"])
                    rewritten = {keys.get(str(identity), str(identity)): value for identity, value in old.items()}
                    if len(rewritten) != len(old):
                        raise ValueError("Cutscene identity destination collision")
                    record["_played_cutscene"] = json.dumps(rewritten, separators=(",", ":"))
                    line = json.dumps(record, separators=(",", ":")) + ("\r\n" if line.endswith("\r\n") else "\n" if line.endswith("\n") else "")
                    changes.append({"operation": "map_cutscene_keys", "path": name})
                lines.append(line)
            converted[name] = "".join(lines).encode()
        elif name != "local_player_id.txt":
            if name in converted:
                raise ValueError("Character destination collision")
            converted[name] = payload
    result = {**manifest, "transport": target_transport, "identity_mapping": {player: identity_mapping[str(identity)] for player, identity in manifest["identity_mapping"].items()}, "validated_transport_conversion": source_transport != target_transport, "validated_mappings": {f"{old}:{new}": True for old, new in identity_mapping.items()}}
    result["setup_audit"] = list(manifest.get("setup_audit", [])) + [{"operation": "explicit_v30_identity_conversion", "source_sha256": hashlib.sha256(blob).hexdigest(), "source_transport": source_transport, "target_transport": target_transport, "identity_mapping": identity_mapping, "changes": changes, "validation": "Inspected v30 identity locations; a normal game load must still verify behavioral compatibility"}]
    return _pack(converted, result), result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Explicit, offline save package conversion; original ZIP remains unchanged")
    parser.add_argument("archive", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--mapping", required=True, type=Path, help="JSON object: old persistent identity to new persistent identity")
    parser.add_argument("--transport", required=True, choices=["enet", "steam"])
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit("Output already exists; choose a new path")
    data = args.archive.read_bytes()
    embedded = json.loads(_archive_files(data)["ashad-fixture.json"])
    result, manifest = convert_archive(data, embedded, json.loads(args.mapping.read_text()), args.transport)
    args.output.write_bytes(result)
    args.output.with_suffix(".manifest.json").write_text(json.dumps(manifest, indent=2))
    print(args.output)
