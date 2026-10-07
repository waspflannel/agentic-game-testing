"""Private framed game protocol. Never retry a possibly executed mutation."""
from __future__ import annotations

import asyncio
import json
import secrets

MAX_MESSAGE = 1_048_576


class GameBridge:
    def __init__(self, port=18740):
        self.port = port
        self.token = secrets.token_urlsafe(32)
        self.lock = asyncio.Lock()

    async def request(self, operation, arguments=None):
        async with self.lock:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self.port, limit=MAX_MESSAGE), 2)
            try:
                payload = json.dumps({"token": self.token, "operation": operation,
                                      "arguments": arguments or {}}, allow_nan=False).encode() + b"\n"
                if len(payload) > MAX_MESSAGE:
                    raise ValueError("Game request exceeds frame limit")
                writer.write(payload)
                await asyncio.wait_for(writer.drain(), 2)
                raw = await asyncio.wait_for(reader.readline(), 4)
                if not raw.endswith(b"\n") or len(raw) > MAX_MESSAGE:
                    raise ValueError("Incomplete or oversized game response")
                result = json.loads(raw)
                if not isinstance(result, dict):
                    raise ValueError("Invalid game response")
                return result
            finally:
                writer.close()
                await writer.wait_closed()
