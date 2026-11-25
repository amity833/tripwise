"""`python -m tripwise`: load ../.env (if present), validate config, serve."""

import sys
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

from .config import ConfigError, load_config
from .main import build_app


def run() -> None:
    load_dotenv(Path(__file__).resolve().parents[2] / ".env")  # repo-root .env; real env vars win
    try:
        config = load_config()
    except ConfigError as e:
        sys.exit(str(e))
    uvicorn.run(build_app(config), host="0.0.0.0", port=config.port, log_level="warning")


if __name__ == "__main__":
    run()
