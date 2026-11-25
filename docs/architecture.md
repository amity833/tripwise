# Architecture

## Module map (one line each)

**backend/tripwise/core**
- `conversation_service.py`: the single entry point for channels. User slots, chat history (list, open, delete, limit), serial per-conversation queue, event dedup, input caps, rate limit, outage simulation, the "yes/cancel" text fallback, and button actions.
- `agent.py`: LLM → tools → LLM loop, capped at 6 rounds, returns only the new transcript entries.
- `proposals.py`: create, confirm, cancel, supersede. Authorization (user and conversation), expiry, atomic confirm.
- `policy.py`: pure policy engine (hard blocks and soft warnings). Re-run on propose and confirm.
- `history.py`: trims history to a token budget by whole turns, and builds the text-only UI history.
- `prompt.py`: the system prompt (today's date, timezone, rules).
- `quota.py`: per-user rate limiter and the global daily LLM cap.
- `trips.py`: the text summary of booked trips.
- `models.py`: shared dataclasses, including the channel-agnostic `AssistantReply`.

**backend/tripwise/llm**: `base.py` (protocol and typed errors) · `gemini.py` (REST client, 25 s timeout, one retry) · `dev_fake.py` (keyword demo model) · `scripted.py` (scripted fake for tests).

**backend/tripwise/tools**: `registry.py` (registry that validates every call with pydantic and turns failures into tool results) · `definitions.py` (the six tools).

**backend/tripwise/data**: `airports.py`, `flights.py` (deterministic, ids encode route/date/index/cabin so they can be re-derived), `hotels.py` and `hotels.json`, `policy.json`, `dates.py`.

**backend/tripwise/store**: `repos.py` (protocol) · `memory.py` · `postgres.py` (asyncpg) · `migrations.py` (plain SQL).

**backend/tripwise/channels**: `web/api.py` (REST, session header, pydantic) · `telegram/adapter.py` (updates, buttons, commands, polling, webhook) · `telegram/format.py` (HTML escaping, splitting, card rendering) · `telegram/api.py`.

**backend/tripwise**: `app.py` (wiring), `server.py` (FastAPI app: static files, `/health`, routes), `main.py` (startup, Telegram mode), `config.py` (env validation), `__main__.py`.

Async model: everything runs on one asyncio event loop. The per-conversation queue is an `asyncio.Lock` per conversation (FIFO), and `MemoryRepo` state changes have no `await` between check and write, which is what makes them atomic.

**web/src**: `App.tsx` (all UI states), `ChatsPanel.tsx` (chat list and delete confirmation), `ProposalCard.tsx`, `api.ts` (typed client, session id).

## Request lifecycle: a Telegram message to a reply

1. Telegram POSTs to `/telegram/webhook`. The secret header is checked with a timing-safe comparison.
2. `adapter.handle_update` validates the payload shape and **claims** `update_id` in `processed_events`. A duplicate returns immediately.
3. The adapter checks that the user has (or can take) a slot, then builds a context (`telegram:<id>`, current conversation) and starts the typing indicator.
4. `ConversationService.handle_message` enqueues the job on that conversation's queue, so earlier messages finish first.
5. The user message is **saved first**. If it is "yes" or "cancel" and a proposal is pending, that is handled deterministically and the LLM is skipped.
6. Otherwise `run_agent` sends the trimmed transcript and the tool specs to the LLM. Each tool call is validated with pydantic and executed. Results go back to the LLM until it answers or hits 6 rounds.
7. New transcript entries (including tool calls and results) are persisted. If `propose_booking` ran, the pending proposal is attached to the reply.
8. The adapter sends the text (escaped, split at 4096) and, for a proposal, a card with ✅ ✏️ ❌ buttons. Sends are retried once, and failures are logged but never lose the persisted reply.
9. The event is marked done. If anything threw, the claim is **released** and the webhook returns 500, so Telegram's retry is processed.

A button press goes through the same adapter, then `handle_action`, which calls `confirm_proposal`, `cancel_proposal` or `supersede_proposal` directly. The LLM is never involved.
