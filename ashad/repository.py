"""Read-only retrieval from the exact source snapshot; no shell exposed to models."""
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

    def _files(self, query=""):
        result = []
        for p in self.root.rglob("*"):
            if p.is_file():
                try:
                    self.resolve(str(p.relative_to(self.root)))
                except ValueError:
                    continue
                rel = p.relative_to(self.root).as_posix()
                if query.casefold() in rel.casefold():
                    result.append({"path": rel, "bytes": p.stat().st_size})
        result.sort(key=lambda x: x["path"])
        return result

    def files(self, query="", offset=0, limit=200):
        result = self._files(query)
        return {"files": result[offset:offset + min(limit, 500)], "total": len(result)}

    def read(self, path, start=1, limit=200):
        p = self.resolve(path)
        if not p.is_file():
            raise ValueError("Source file not found")
        if self.hashes is not None and sha256_file(p) != self.hashes[p.relative_to(self.root).as_posix()]:
            raise ValueError("Pinned source file changed; repository context is invalid")
        if p.stat().st_size > 8 * 1024**2:
            return {"path": path, "sha256": sha256_file(p), "bytes": p.stat().st_size, "message": "Large binary asset; retrieve metadata or smaller text definition"}
        try:
            lines = p.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            return {"path": path, "sha256": sha256_file(p), "bytes": p.stat().st_size, "binary": True}
        start = max(1, int(start))
        limit = max(1, min(int(limit), 500))
        return {"path": path, "start": start, "total_lines": len(lines), "text": "\n".join(f"{i+1}: {s}" for i, s in enumerate(lines) if start-1 <= i < start-1+limit)}

    def search(self, query, offset=0, limit=100):
        if not query:
            raise ValueError("Search text is required")
        matches = []
        for item in self._files():
            p = self.resolve(item["path"])
            if p.stat().st_size > 2 * 1024**2:
                continue
            try:
                if self.hashes is not None and sha256_file(p) != self.hashes[item["path"]]:
                    raise ValueError("Pinned source changed during repository search")
                lines = p.read_text(encoding="utf-8").splitlines()
            except UnicodeDecodeError:
                continue
            for lineno, line in enumerate(lines, 1):
                if query.casefold() in line.casefold():
                    matches.append({"path": item["path"], "line": lineno, "text": line[:1500]})
        return {"matches": matches[offset:offset+min(limit, 200)], "total": len(matches)}

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
