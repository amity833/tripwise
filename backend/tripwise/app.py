"""Wiring: builds the repo, the LLM client and the conversation service from config."""

from __future__ import annotations

from .config import Config
from .core.conversation_service import ConversationService
from .core.quota import QuotaLlm, RateLimiter
from .llm.base import LlmClient
from .llm.dev_fake import DevFakeLlm
from .llm.gemini import GeminiClient
from .store.memory import MemoryRepo
from .store.repos import Repo
from .tools import default_tools


async def build_repo(config: Config) -> Repo:
    if not config.database_url:
        return MemoryRepo()
    from .store.postgres import PostgresRepo  # imported lazily so the in-memory path needs no database driver at runtime

    return await PostgresRepo.connect(config.database_url)


def build_llm(config: Config) -> LlmClient:
    if config.llm_provider == "gemini":
        assert config.gemini_api_key
        return QuotaLlm(GeminiClient(config.gemini_api_key, config.llm_model), config.daily_llm_cap)
    return DevFakeLlm(config.default_tz)


def build_service(config: Config, repo: Repo, llm: LlmClient | None = None) -> ConversationService:
    return ConversationService(
        repo=repo, llm=llm or build_llm(config), tools=default_tools(), tz=config.default_tz, limiter=RateLimiter(20, 60_000),
        max_conversations=config.max_conversations, max_users=config.max_users,
    )
