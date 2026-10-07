"""SQLite metadata plus fsynced append-only UTF-8 ledgers. No secrets."""
from __future__ import annotations
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid


def uid(prefix=""):
    return prefix + uuid.uuid4().hex


def redact(value):
    if isinstance(value, dict):
        return {k: ("[REDACTED]" if k.lower() in {"api_key", "token", "access_token", "refresh_token", "id_token", "client_secret", "password", "authorization", "secret", "encrypted_content"} else redact(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        value = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[REDACTED]", value)
        return re.sub(r"(?i)\bBearer\s+[A-Za-z0-9._~+/-]+=*", "Bearer [REDACTED]", value)
    return value


class Store:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "runs").mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.db = sqlite3.connect(self.root / "ashad.sqlite3", check_same_thread=False, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
        PRAGMA journal_mode=WAL; PRAGMA synchronous=FULL;
        CREATE TABLE IF NOT EXISTS records (kind TEXT, id TEXT, data TEXT NOT NULL, updated REAL NOT NULL, PRIMARY KEY(kind,id));
        CREATE TABLE IF NOT EXISTS leases (kind TEXT, resource_id TEXT, run_id TEXT NOT NULL, PRIMARY KEY(kind,resource_id));
        CREATE TABLE IF NOT EXISTS events (run_id TEXT, seq INTEGER, id TEXT UNIQUE, data TEXT NOT NULL, PRIMARY KEY(run_id,seq));
        CREATE TABLE IF NOT EXISTS commands (scope TEXT, id TEXT, result TEXT NOT NULL, PRIMARY KEY(scope,id));
        """)

    @contextlib.contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.execute("COMMIT")
            except BaseException:
                self.db.execute("ROLLBACK")
                raise

    def put(self, kind, data, key=None):
        key = key or data["id"]
        with self.lock:
            self.db.execute("INSERT INTO records VALUES (?,?,?,?) ON CONFLICT(kind,id) DO UPDATE SET data=excluded.data,updated=excluded.updated", (kind, key, json.dumps(redact(data), ensure_ascii=False), time.time()))
        return data

    def get(self, kind, key, default=None):
        with self.lock:
            row = self.db.execute("SELECT data FROM records WHERE kind=? AND id=?", (kind, key)).fetchone()
        return json.loads(row[0]) if row else default

    def require(self, kind, key):
        value = self.get(kind, key)
        if value is None:
            raise ValueError(f"Unknown {kind}: {key}")
        return value

    def list(self, kind):
        with self.lock:
            rows = self.db.execute("SELECT data FROM records WHERE kind=? ORDER BY updated DESC", (kind,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def delete(self, kind, key):
        with self.lock:
            self.db.execute("DELETE FROM records WHERE kind=? AND id=?", (kind, key))

    def run_dir(self, run_id):
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", run_id):
            raise ValueError("Invalid run ID")
        p = self.root / "runs" / run_id
        p.mkdir(exist_ok=True)
        return p

    def event(self, run_id, kind, payload=None, **fields):
        with self.lock:
            row = self.db.execute("SELECT MAX(seq) FROM events WHERE run_id=?", (run_id,)).fetchone()
            seq = (row[0] or 0) + 1
            previous = self.db.execute("SELECT data FROM events WHERE run_id=? ORDER BY seq DESC LIMIT 1", (run_id,)).fetchone()
            prev_hash = json.loads(previous[0])["hash"] if previous else None
            event = redact({"id": uid("evt_"), "version": 1, "run_id": run_id, "seq": seq, "timestamp": time.time(), "monotonic": time.monotonic(), "kind": kind, "payload": payload or {}, "previous_hash": prev_hash, **fields})
            event["hash"] = hashlib.sha256(json.dumps(event, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            raw = json.dumps(event, ensure_ascii=False)
            # Ledger is authoritative; recovery reconciles a crash between append and DB index.
            with (self.run_dir(run_id) / "ledger.jsonl").open("a", encoding="utf-8", newline="\n") as f:
                f.write(raw + "\n")
                f.flush()
                os.fsync(f.fileno())
            self.db.execute("INSERT INTO events VALUES (?,?,?,?)", (run_id, seq, event["id"], raw))
            return event

    def recover_ledger(self, run_id):
        path = self.run_dir(run_id) / "ledger.jsonl"
        if not path.exists():
            return []
        errors = []
        prev = None
        with self.lock, path.open(encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                try:
                    event = json.loads(line)
                    given = event.pop("hash")
                    expected = hashlib.sha256(json.dumps(event, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                    event["hash"] = given
                    if given != expected or event["previous_hash"] != prev:
                        raise ValueError("Hash chain mismatch")
                    prev = given
                    self.db.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?)", (run_id, event["seq"], event["id"], json.dumps(event)))
                except (ValueError, KeyError) as exc:
                    errors.append(f"Ledger line {lineno}: {exc}")
                    break
        return errors

    def events(self, run_id, after=0, limit=200, search=""):
        with self.lock:
            rows = self.db.execute("SELECT data FROM events WHERE run_id=? AND seq>? ORDER BY seq", (run_id, after)).fetchall()
        entries = [json.loads(r[0]) for r in rows]
        if search:
            entries = [e for e in entries if search.casefold() in json.dumps(e, ensure_ascii=False).casefold()]
        return entries[:limit]

    def count_events(self, run_id):
        with self.lock:
            return self.db.execute("SELECT COUNT(*) FROM events WHERE run_id=?", (run_id,)).fetchone()[0]

    def leases(self, run_id=None):
        with self.lock:
            rows = self.db.execute("SELECT * FROM leases" + (" WHERE run_id=?" if run_id else ""), (run_id,) if run_id else ()).fetchall()
        return [dict(r) for r in rows]

    def reserve(self, run_id, resources):
        with self.transaction() as db:
            for kind, resource in resources:
                row = db.execute("SELECT run_id FROM leases WHERE kind=? AND resource_id=?", (kind, resource)).fetchone()
                if row and row[0] != run_id:
                    return False
            for kind, resource in resources:
                db.execute("INSERT OR IGNORE INTO leases VALUES (?,?,?)", (kind, resource, run_id))
        return True

    def release(self, run_id):
        with self.lock:
            self.db.execute("DELETE FROM leases WHERE run_id=?", (run_id,))

    def command(self, scope, key, result=None):
        with self.lock:
            if result is not None:
                self.db.execute("INSERT OR IGNORE INTO commands VALUES (?,?,?)", (scope, key, json.dumps(result)))
            row = self.db.execute("SELECT result FROM commands WHERE scope=? AND id=?", (scope, key)).fetchone()
        return json.loads(row[0]) if row else None

    def close(self):
        self.db.close()
