"""Pinned source snapshot: path checks and catalog."""
from pathlib import Path
import hashlib
import json


def sha256_file(path):
    with Path(path).open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


class Repository:
    def __init__(self, build):
        self.root = Path(build["source_root"]).resolve()
        if not self.root.is_dir():
            raise ValueError("Pinned repository snapshot is unavailable")
        hashes = self.root.parent / "source-hashes.json"
        self.hashes = json.loads(hashes.read_text(encoding="utf-8-sig")) if hashes.is_file() else None

    def resolve(self, relative):
        p = (self.root / relative).resolve()
        if not p.is_relative_to(self.root) or any(part in {".git", ".godot", "userdata"} for part in p.relative_to(self.root).parts):
            raise ValueError("Source path is outside the pinned repository")
        if p.name.startswith(".env") or p.suffix in {".pem", ".key"}:
            raise ValueError("Credentials are not repository context")
        if self.hashes is not None and p.relative_to(self.root).as_posix() not in self.hashes:
            raise ValueError("File is not part of the pinned source inventory")
        return p

    def catalog(self, build):
        path = build.get("catalog_path")
        if path and Path(path).is_file():
            return json.loads(Path(path).read_text(encoding="utf-8-sig"))
        goals = []
        for p in self.root.rglob("*.tres"):
            rel = p.relative_to(self.root).as_posix()
            if any(s in rel for s in ("quests/", "items/", "biomes/")):
                goals.append({"id": p.stem, "path": rel})
        return {"goals": goals}


def catalog_ids(catalog):
    result = set()
    def walk(value):
        if isinstance(value, dict):
            for key in ("id", "quest_id", "item_id", "goal_id", "resource_name"):
                if isinstance(value.get(key), str):
                    result.add(value[key])
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)
    walk(catalog)
    return result
