from __future__ import annotations

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


class ConfigError(Exception):
    pass


@dataclass(frozen=True)
class Config:
    port: int = 3000
    llm_provider: str = "fake"
    llm_model: str = "gemini-3.5-flash-lite"
    gemini_api_key: str | None = None
    database_url: str | None = None
    public_url: str | None = None
    telegram_bot_token: str | None = None
    telegram_webhook_secret: str | None = None
    default_tz: str = "UTC"
    daily_llm_cap: int = 2000
    max_users: int = 10
    max_conversations: int = 5


def load_config(env: Mapping[str, str] | None = None) -> Config:
    """Fails fast with a readable message listing every problem."""
    env = os.environ if env is None else env
    # `KEY=` in a .env file means "unset", not an empty string.
    e = {k: v for k, v in env.items() if v != ""}
    issues: list[tuple[str, str]] = []

    def integer(key: str, default: int) -> int:
        if key not in e:
            return default
        try:
            return int(float(e[key]))
        except ValueError:
            issues.append((key, "must be a number"))
            return default

    port = integer("PORT", 3000)
    cap = integer("DAILY_LLM_CAP", 2000)
    max_users = integer("MAX_USERS", 10)
    max_conversations = integer("MAX_CONVERSATIONS", 5)
    for key, value in (("MAX_USERS", max_users), ("MAX_CONVERSATIONS", max_conversations)):
        if value < 1:
            issues.append((key, "must be at least 1"))

    provider = e.get("LLM_PROVIDER", "fake")
    if provider not in ("fake", "gemini"):
        issues.append(("LLM_PROVIDER", "must be one of: fake, gemini"))

    db = e.get("DATABASE_URL")
    if db is not None and not re.match(r"^postgres(ql)?://", db):
        issues.append(("DATABASE_URL", "must start with postgres:// or postgresql://"))

    public = e.get("PUBLIC_URL")
    if public is not None:
        u = urlparse(public)
        if u.scheme not in ("http", "https") or not u.netloc:
            issues.append(("PUBLIC_URL", "must be a valid URL"))

    tz = e.get("DEFAULT_TZ", "UTC")
    try:
        ZoneInfo(tz)
    except Exception:
        issues.append(("DEFAULT_TZ", f'unknown timezone "{tz}"'))

    if not issues:  # cross-field rules only make sense once the fields themselves are valid
        if provider == "gemini" and not e.get("GEMINI_API_KEY"):
            issues.append(("GEMINI_API_KEY", "required when LLM_PROVIDER=gemini"))
        if public and not e.get("TELEGRAM_WEBHOOK_SECRET"):
            issues.append(("TELEGRAM_WEBHOOK_SECRET", "required when PUBLIC_URL is set"))

    if issues:
        lines = "\n".join(f"  - {k}: {m}" for k, m in issues)
        raise ConfigError(f"Invalid environment configuration:\n{lines}")

    return Config(
        port=port,
        llm_provider=provider,
        llm_model=e.get("LLM_MODEL", "gemini-3.5-flash-lite"),
        gemini_api_key=e.get("GEMINI_API_KEY"),
        database_url=db,
        public_url=public,
        telegram_bot_token=e.get("TELEGRAM_BOT_TOKEN"),
        telegram_webhook_secret=e.get("TELEGRAM_WEBHOOK_SECRET"),
        default_tz=tz,
        daily_llm_cap=cap,
        max_users=max_users,
        max_conversations=max_conversations,
    )
