"""Packaged API entrypoint. CWD is writable instance, imports are read-only."""

import hmac
import os
from urllib.parse import urlsplit


class ReadOnlyGuard:
    """Outer bearer/origin boundary; existing bridge keeps its HMAC challenge."""

    def __init__(self, app, token, *, writes=False, owned_origin=None):
        self.app = app
        self.authorization = ("Bearer " + token).encode("ascii")
        self.writes = writes
        self.owned_origin = owned_origin.encode("ascii") if owned_origin else None
        self.owned_host = urlsplit(owned_origin).netloc.encode("ascii") if owned_origin else None

    def origin_allowed(self, headers, *, websocket=False):
        if self.owned_origin is None:
            return True
        if headers.get(b"host") != self.owned_host:
            return False
        origin = headers.get(b"origin")
        return origin == self.owned_origin or (origin is None and not websocket)

    async def __call__(self, scope, receive, send):
        if scope["type"] == "websocket":
            headers = dict(scope.get("headers", []))
            if (not self.writes or scope.get("path") != "/browser-bridge"
                    or not self.origin_allowed(headers, websocket=True)
                    or not hmac.compare_digest(headers.get(b"authorization", b""), self.authorization)):
                await send({"type": "websocket.close", "code": 1008})
                return
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            authorized = hmac.compare_digest(headers.get(b"authorization", b""), self.authorization)
            configuration_read = (scope["method"] == "POST"
                                  and scope.get("path") == "/api/local-ui/configuration/read")
            status = 401 if not authorized else (403 if not self.writes
                     and scope["method"] not in {"GET", "HEAD", "OPTIONS"}
                     and not configuration_read else 0)
            if authorized and not self.origin_allowed(headers):
                status = 403
            if status:
                await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", b"application/json")]})
                await send({"type": "http.response.body", "body": b'{"error":"desktop_read_only_boundary"}'})
                return
        await self.app(scope, receive, send)


def main():
    # This entrypoint is launched only by the isolated supervisor.
    if os.environ.get("RECRUITOPS_ENV") != "desktop-isolated":
        raise SystemExit("isolated supervisor required")
    import uvicorn
    from apps.api.main import app, get_storage_engine
    from fastapi import Request
    from fastapi.responses import JSONResponse
    from sqlalchemy import text

    token = os.environ["RECRUITOPS_API_TOKEN"]
    instance = os.environ["RECRUITOPS_DESKTOP_INSTANCE_ID"]
    run_id = os.environ["RECRUITOPS_DESKTOP_RUN_ID"]
    writes = (os.environ.get("RECRUITOPS_WRITE_ENABLED") == "true"
              and os.environ.get("RECRUITOPS_DESKTOP_WRITE_OPTIN") == instance)

    @app.middleware("http")
    async def isolated_guard(request: Request, call_next):
        if request.url.path == "/desktop-runtime/ready":
            return desktop_ready()
        return await call_next(request)

    def desktop_ready():
        try:
            with get_storage_engine().connect() as connection:
                connection.execute(text("SELECT 1"))
        except Exception:
            return JSONResponse({"status": "not_ready"}, status_code=503)
        return JSONResponse({"instance_id": instance, "run_id": run_id, "status": "ready",
                             "writes": writes, "websocket": writes})

    port = int(os.environ["RECRUITOPS_API_PORT"])
    uvicorn.run(ReadOnlyGuard(app, token, writes=writes, owned_origin=f"http://127.0.0.1:{port}"),
                host="127.0.0.1", port=port, access_log=False, ws="websockets")


if __name__ == "__main__":
    main()
