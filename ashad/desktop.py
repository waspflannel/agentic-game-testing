"""Owned-loopback transport for the desktop shell, not a browser-mode bypass."""
import secrets
import socket

from starlette.responses import JSONResponse

PROTOCOL = "ashad-desktop/1"


def bind_loopback():
    """Keep the ephemeral port reserved until the server takes this same socket."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        listener.setblocking(False)
        return listener
    except BaseException:
        listener.close()
        raise


class DesktopBoundary:
    """Secrets stay in main-process memory; main injects headers only for this origin.

    The main-only key is separate from renderer requests. A shutdown callback must
    drain admission and confirm all ownership release before returning true.
    """
    def __init__(self, app, *, port, session_key, owner_key, instance_id, shutdown):
        for key in (session_key, owner_key):
            if not isinstance(key, str) or len(key) < 43 or not key.isascii():
                raise ValueError("Desktop keys require at least 256 bits of random material")
        if session_key == owner_key:
            raise ValueError("Desktop session and owner keys must differ")
        self.app = app
        self.authority = f"127.0.0.1:{port}"
        self.session_key, self.owner_key = session_key, owner_key
        self.instance_id, self.shutdown = instance_id, shutdown

    async def __call__(self, scope, receive, send):
        if scope["type"] == "lifespan":
            return await self.app(scope, receive, send)
        if scope["type"] != "http":
            await send({"type": "websocket.close", "code": 1008})
            return
        headers = {}
        for name, value in scope.get("headers", []):
            # Reject duplicate boundary headers rather than disagree with a proxy/parser.
            if name in headers and name in {b"host", b"origin", b"x-ashad-desktop", b"x-ashad-owner"}:
                return await self.reply(scope, receive, send, 403, "Ambiguous desktop headers")
            headers[name] = value.decode("latin-1")
        if headers.get(b"host") != self.authority or headers.get(b"origin", f"http://{self.authority}") != f"http://{self.authority}":
            return await self.reply(scope, receive, send, 403, "Unexpected desktop origin")
        administrative = scope["path"].startswith("/_desktop/")
        supplied = headers.get(b"x-ashad-owner" if administrative else b"x-ashad-desktop", "")
        expected = self.owner_key if administrative else self.session_key
        if not secrets.compare_digest(supplied.encode("latin-1"), expected.encode("ascii")):
            return await self.reply(scope, receive, send, 401, "Owned desktop authentication required")
        if not administrative:
            return await self.app(scope, receive, send)
        if scope["path"] == "/_desktop/ready" and scope["method"] == "GET":
            response = JSONResponse({"protocol": PROTOCOL, "instance_id": self.instance_id})
        elif scope["path"] == "/_desktop/shutdown" and scope["method"] == "POST":
            allowed = await self.shutdown()
            response = JSONResponse({"drained": allowed}, status_code=200 if allowed else 409)
        else:
            response = JSONResponse({"detail": "Unknown desktop operation"}, status_code=404)
        response.headers["Cache-Control"] = "no-store"
        await response(scope, receive, send)

    @staticmethod
    async def reply(scope, receive, send, status, message):
        await JSONResponse({"detail": message}, status_code=status,
                           headers={"Cache-Control": "no-store"})(scope, receive, send)
