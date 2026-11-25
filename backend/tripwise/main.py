"""Production entry: builds everything at startup (lifespan) and runs Telegram in webhook or polling mode."""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import AsyncIterator
from pathlib import Path

from fastapi import FastAPI

from .app import build_repo, build_service
from .channels.telegram.adapter import TelegramAdapter
from .channels.telegram.api import HttpTelegramApi
from .config import Config
from .logger import log
from .server import create_app


def find_static_dir() -> Path | None:
    here = Path(__file__).resolve().parent  # backend/tripwise
    env = os.environ.get("WEB_DIST")
    candidates = [Path(env)] if env else []
    candidates += [here.parent.parent / "web" / "dist", Path.cwd() / "web" / "dist", Path.cwd().parent / "web" / "dist"]
    return next((d for d in candidates if d.is_dir()), None)


def build_app(config: Config) -> FastAPI:
    static_dir = find_static_dir()

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        repo = await build_repo(config)
        service = build_service(config, repo)
        app.state.repo, app.state.service = repo, service

        tg_api: HttpTelegramApi | None = None
        poll_task: asyncio.Task[None] | None = None
        if config.telegram_bot_token:
            tg_api = HttpTelegramApi(config.telegram_bot_token)
            tg = TelegramAdapter(tg_api, service, repo, config.telegram_webhook_secret)
            if config.public_url:
                try:
                    await tg.register(config.public_url)
                except Exception as e:
                    log("error", "telegram webhook registration failed", err=str(e))
                app.state.webhook = tg.webhook
                log("info", "telegram webhook mode")
            else:
                # Dev: polling. Use a SEPARATE dev bot token: polling with the production token deletes its webhook.
                with contextlib.suppress(Exception):
                    await tg.register()
                poll_task = asyncio.create_task(tg.poll())
                log("info", "telegram polling mode (dev bot)")
        else:
            log("info", "telegram disabled (no TELEGRAM_BOT_TOKEN)")

        log("info", "listening", port=config.port, provider=config.llm_provider,
            store="postgres" if config.database_url else "memory", web=str(static_dir) if static_dir else "not built")
        try:
            yield
        finally:
            if poll_task:
                poll_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await poll_task
            if tg_api:
                await tg_api.aclose()
            close = getattr(repo, "close", None)
            if close:
                await close()

    return create_app(config, static_dir=static_dir, lifespan=lifespan)
