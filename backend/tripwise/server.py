"""HTTP layer: /health, the Telegram webhook, the web REST API, and the built web app (with SPA fallback)."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse

from .channels.web.api import handle_api
from .config import Config
from .core.conversation_service import ConversationService
from .logger import log
from .store.repos import Repo

WebhookHandler = Callable[[Request], Awaitable[Response]]


def create_app(
    config: Config, service: ConversationService | None = None, repo: Repo | None = None,
    webhook: WebhookHandler | None = None, static_dir: str | Path | None = None,
    lifespan: Callable[[FastAPI], AbstractAsyncContextManager[None]] | None = None,
) -> FastAPI:
    """Dependencies can be passed in (tests) or filled into `app.state` later by a lifespan (production)."""
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    app.state.config = config
    app.state.service = service
    app.state.repo = repo
    app.state.webhook = webhook
    app.state.static_root = Path(static_dir).resolve() if static_dir else None

    @app.get("/health")
    async def health(request: Request) -> dict[str, Any]:
        c: Config = request.app.state.config
        return {
            "status": "ok", "store": "postgres" if c.database_url else "memory", "llm": c.llm_provider,
            "telegram": request.app.state.webhook is not None,
        }

    @app.post("/telegram/webhook")
    async def telegram_webhook(request: Request) -> Response:
        handler: WebhookHandler | None = request.app.state.webhook
        if handler is None:
            return JSONResponse({"error": "Not found"}, status_code=404)
        return await handler(request)

    @app.api_route("/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def web_api(request: Request) -> Response:
        return await handle_api(request, request.app.state.service)

    @app.api_route("/{full_path:path}", methods=["GET", "HEAD"])
    async def static(full_path: str, request: Request) -> Response:
        root: Path | None = request.app.state.static_root
        if root is not None:
            file = (root / (full_path or "index.html")).resolve()
            if not file.is_relative_to(root):
                return Response(status_code=403)
            if not file.is_file():
                file = root / "index.html"  # SPA fallback
            if file.is_file():
                return FileResponse(file)
        return JSONResponse({"error": "Not found"}, status_code=404)

    @app.exception_handler(Exception)
    async def unhandled(_request: Request, exc: Exception) -> Response:
        log("error", "unhandled http error", err=str(exc))
        return Response(status_code=500)

    return app
