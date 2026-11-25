# Tripwise: a corporate travel assistant on Telegram and the web

<!-- After pushing to GitHub, uncomment and fill in:
![CI](https://github.com/<you>/<repo>/actions/workflows/ci.yml/badge.svg)
-->

An AI assistant that plans business trips. It finds flights and hotels, checks them against company policy, and **proposes** an itinerary. Nothing is booked until the user confirms. One **Python (FastAPI)** backend serves both a **Telegram bot** and a **React + TypeScript web app**.

## Try it

| | |
|---|---|
| Web app | `TODO: https://<service>.onrender.com` |
| Telegram bot | `TODO: https://t.me/<bot>` |
| Demo video | `TODO: <unlisted link>` |

**Access:** no login, invite, or API key needed. On Telegram, open the bot in a private chat and press **Start**. The web app creates an anonymous session automatically.

- The demo serves **10 users at a time**. Each Telegram account and each web browser (or incognito window) counts as one, and a place opens after 24 hours without activity.
- The service runs on a free tier, so the first request after an idle period can be slow.
- The two channels are separate conversations (account linking is not implemented).
- Supported cities: Tel Aviv, Berlin, London, New York, San Francisco, Paris, Amsterdam, Madrid, Barcelona, Rome, Athens, Lisbon, Dubai. All flights and hotels are mock data.
- Please don't enter personal data: free-tier Gemini prompts may be used by Google.

## Reviewer guide (2 minutes)

| # | Do this (Telegram or web) | What you should see |
|---|---|---|
| 1 | *"I need flights to Berlin from Tel Aviv next month"* | Flight and hotel options, with the assumed dates spelled out |
| 2 | Follow-up: *"book the second one"* | Context carries over and a **PROPOSED** card appears |
| 3 | Press **✅ Confirm** | The card becomes **BOOKED ✅** with a reference. A double tap books once |
| 4 | `/trips` or **My trips**, then `/new` or **New chat** | Your trips survive a new conversation |
| 5 | *"business class to Berlin from Tel Aviv"*, then *"book the first one"* | **Policy block:** no business class under 6 hours. A compliant alternative is offered |
| 6 | *"flights to Berlin from Tel Aviv tomorrow"*, then book one | The card carries a **policy warning** (booked less than 7 days ahead) |
| 7 | `/simulate_outage` (web: **simulate outage** link), then send any message | **Failure:** a clear error with a reference id and a *Try again* option |
| 8 | Send your request again | **Recovery:** the outage cleared itself after one failure |
| 9 | `/chats` (web: **Chats**), then pick an earlier chat | Its context is back. Chats can be deleted, and each user keeps up to 5 |

## How the requirements are met

| Requirement | Implementation |
|---|---|
| Real messaging integration | Telegram Bot API: webhook in production, long polling in development ([adapter.py](backend/tripwise/channels/telegram/adapter.py)) |
| Defined conversation model | Direct messages only ([why](#conversation-model)) |
| React + TypeScript web app | Chat, history, proposal cards, trips ([web/src](web/src)) |
| Loading, success, and error states | [Web experience](#web-experience) |
| Shared backend, separated channels | `channels/` is isolated from the assistant logic and a lint rule enforces it ([Architecture](#architecture)) |
| Conversation history | Up to 5 chats per user, switch or delete on both channels |
| Follow-ups and isolation | [Conversation handling and storage](#conversation-handling-and-storage) |
| LLM with tools | Gemini with six tools ([The assistant](#the-assistant)) |
| Failure handling | [Reliability](#reliability) |
| Focused tests | [Tests](#tests) |
| Optional enhancements | [Action confirmation and channel-native interactions](#optional-enhancements) |

## Architecture

```
 Telegram ──webhook──▶ channels/telegram ─┐
                                          ├─▶ core/ConversationService ─▶ core/agent ─▶ llm/ (Gemini | Dev fake)
 React app ──REST───▶ channels/web ───────┘          │                         │
                                                     ▼                         ▼
                                              store/ (Memory | Postgres)    tools/ + data/ (mock flights, hotels, policy)
```

| Part of `backend/tripwise` | Responsibility |
|---|---|
| `channels/` | Translate a channel's input into a service call, and render the reply |
| `core/` | Conversation service, agent loop, proposals, policy, rate limits |
| `llm/` | LLM interface, Gemini client, keyword-driven dev model, scripted test fake |
| `tools/`, `data/` | Validated tools over deterministic mock inventory |
| `store/` | Repository interface with in-memory and Postgres implementations |

Both channels call the same `ConversationService` and receive a channel-agnostic `AssistantReply` (text, optional proposal, optional quick replies). `core/`, `tools/`, `llm/`, `store/`, and `data/` **never import from `channels/`**, enforced by a ruff rule (`TID251` in [backend/pyproject.toml](backend/pyproject.toml)) that CI runs.

In production a single Docker container serves the API, the Telegram webhook, and the built web app.

More: [docs/architecture.md](docs/architecture.md) (module map and request lifecycle) and [docs/decisions.md](docs/decisions.md).

## Conversation model

**Telegram direct messages only.**

- Travel plans and bookings are personal, so they don't belong in a group chat.
- One private chat per user is the simplest unit of isolation: one chat, one user, one current conversation.
- A reviewer can try the bot from a link, with nothing to install into a workspace.
- Groups get a polite "I only work in direct messages" reply.

A *user* (`telegram:<id>` or `web:<hash of session id>`) owns many *conversations*, and **owns all bookings**. `/new` and **New chat** start a fresh conversation without losing trips.

**Chat history:** a user keeps up to 5 chats. `/chats` (web: **Chats**) switches to an earlier one, with its full context, or deletes one after confirmation. At the limit a new chat is refused until one is deleted, and an empty chat is reused. The current chat is the one most recently opened or written to. Telegram shows one continuous thread, so switching there posts the chat's last reply as a recap.

Telegram commands: `/start`, `/help`, `/new`, `/chats`, `/trips`, `/simulate_outage`.

## Web experience

| State | What the user sees |
|---|---|
| Loading history | Skeleton placeholder |
| Empty chat | Intro text and suggested prompts |
| Sending | *Sending…* (or *Queued…* behind an earlier message) and a typing indicator |
| Success | The reply, a proposal card, then **BOOKED ✅** after confirming |
| Send failed | *Failed to send* and a banner with **Retry**, which reuses the message id so it never runs twice |
| History failed to load | Banner with **Retry** |
| Chat limit reached | The chat list opens, asking to delete one |
| Demo full | A message explaining when to try again |
| Assistant failed | An explanation with a reference id and a **Try again** chip |

Reloading the page restores the current conversation's text and any pending proposal.

## The assistant

A small agent loop: LLM → tools → LLM, capped at 6 rounds.

| Tool | Purpose |
|---|---|
| `resolve_date` | Turn "tomorrow", "next month", "in 10 days" into a real date |
| `search_flights` | Search mock flights |
| `search_hotels` | Search mock hotels |
| `check_policy` | Check a flight or hotel against company policy |
| `propose_booking` | Create a **pending** proposal |
| `list_bookings` | List the user's confirmed trips |

**Booking is not a tool.** Confirming is deterministic code behind a button or an explicit "yes".

Policy: economy only on flights under 6 hours (block), nightly hotel cap by city tier of $300 / $200 / $150 (block), warning when booking less than 7 days ahead.

## Conversation handling and storage

- **Follow-ups:** the full transcript, including tool calls and results, is stored, so "book the second one" resolves against earlier search results. Old turns are trimmed to a token budget without splitting a tool call from its result.
- **Isolation:** every request checks that the conversation belongs to the caller. A proposal can only be acted on by its owner, within its own conversation (a button on an older Telegram card acts in that card's chat).
- **Ordering:** messages and button presses are processed one at a time per conversation, in arrival order.
- **Duplicates:** Telegram update ids, callback ids, and web message ids go through a claim → done → release-on-failure ledger. Duplicates are ignored, and a failed attempt can be retried.
- **Input limits:** 2,000 characters per message and 20 messages per minute per user.
- **Demo limits:** 10 users holding a slot at once and 5 chats per user. A slot lapses after 24 idle hours; the data is kept.

### Storage

| | In-memory (default) | Postgres (`DATABASE_URL` set) |
|---|---|---|
| Use | Local development and tests | The hosted demo |
| Survives restart | No | Yes |

**In-memory limitations:** all history, proposals, bookings, and dedup records are **lost on restart or sleep**; nothing is **shared between instances**; each conversation keeps only the latest 500 messages; and the other records grow without eviction.

Postgres is used for the demo because a free-tier service that sleeps would otherwise wipe history in the middle of a review.

## Reliability

| Failure | Handling |
|---|---|
| LLM timeout, rate limit, or server error | One retry, then an explanation with a reference id. The user's message is already saved |
| LLM daily quota reached | A specific "try again tomorrow" message, with no retry |
| Storage or code error while handling a message | An explanation that the fault is on our side, with a reference id. The stack trace is logged |
| Tool error or invalid arguments | Returned to the model as a tool result, so it can correct itself or explain |
| Telegram send fails | Retried once, then logged. The reply stays saved |
| Telegram rejects the formatting | Resent as plain text |
| Telegram update processing crashes | Claim released and HTTP 500 returned, so Telegram redelivers |
| Web request fails | Error banner with **Retry** |

## Optional enhancements

**1. Clear action confirmation.** A card previews flights, hotel, total price, and policy verdict, marked **PROPOSED**. The user can **Confirm**, **Change**, or **Cancel**. The status then reads **BOOKED**, **CANCELLED**, **REPLACED** (a newer proposal, or **Change**), or **EXPIRED**. Proposals expire after 30 minutes, only one is live per conversation, policy is re-checked on confirm, and confirming is atomic.

**2. Channel-native interactions.** Telegram uses inline buttons, edits the card in place after an action, shows a typing indicator, and registers a command menu. As a text fallback, replying "yes" or "cancel" acts on the pending proposal. An acknowledgement such as "ok" books nothing, and "yes" only confirms a card that reached the user: otherwise the card is shown first.

## Quickstart (no keys needed)

Requires Python 3.11+ and Node 22+ (Node only builds the web app).

```bash
python3 -m venv backend/.venv && backend/.venv/bin/pip install -e 'backend[dev]'
npm install
npm run dev        # builds the web app, serves UI + API on http://localhost:3000
```

This uses `LLM_PROVIDER=fake`, a keyword-driven stand-in that demonstrates the flow without a real LLM, plus the in-memory store. Try: `flights to Berlin from Tel Aviv`, `book the second one`, `yes`, `my trips`. For natural-language requests, configure Gemini below.

For UI hot reload, run `npm run dev:web` in a second terminal.

## Setup with real services

Copy `.env.example` to `.env` and fill it in. `.env.example` contains no credentials.

| Variable | Purpose |
|---|---|
| `PORT` | HTTP port (default 3000) |
| `LLM_PROVIDER` | `fake` (default) or `gemini` |
| `GEMINI_API_KEY`, `LLM_MODEL` | Google AI Studio key and model name |
| `DAILY_LLM_CAP` | Global daily cap on LLM calls |
| `MAX_USERS`, `MAX_CONVERSATIONS` | Users holding a slot at once, and saved chats per user (defaults: 10 and 5) |
| `DATABASE_URL` | Postgres URL (for example Neon). Empty means in-memory |
| `TELEGRAM_BOT_TOKEN` | Bot token from @BotFather. Empty disables Telegram |
| `PUBLIC_URL`, `TELEGRAM_WEBHOOK_SECRET` | Set both for **webhook** mode. Leave `PUBLIC_URL` empty for **polling** |
| `DEFAULT_TZ` | Timezone used to resolve relative dates |

- **Use a separate bot token locally.** Polling with the production token deletes the production webhook.
- Configuration is validated at startup and lists every problem at once.
- `GET /health` reports the active store, LLM provider, and whether the webhook handler is active.

### Deploy

[render.yaml](render.yaml) is a Render blueprint using the [Dockerfile](Dockerfile). Set the secrets in the Render dashboard; the server registers the Telegram webhook on boot. A free uptime pinger on `/health` helps avoid cold starts.

## Tests

```bash
npm test             # backend (pytest) and web (vitest)
npm run lint         # ruff, including the layering rule
npm run typecheck    # web
```

| Behavior | Where |
|---|---|
| Conversation and user isolation | `test_core.py`, `test_domain.py`, `test_channels.py` |
| Follow-up context | `test_core.py` |
| Serial processing of rapid messages | `test_core.py` |
| LLM, tool, and delivery failures | `test_core.py`, `test_gemini.py`, `test_channels.py` |
| Duplicate incoming events | `test_core.py`, `test_channels.py` |
| Double confirm, expiry, policy re-check | `test_domain.py` |
| Chat history, chat limit, deletion, user slots | `test_chats.py`, [web/src/Chats.test.tsx](web/src/Chats.test.tsx) |
| Storage contract (memory and Postgres) | `test_repo_contract.py` |
| Web retry and safe rendering of replies | [web/src/App.test.tsx](web/src/App.test.tsx) |

Backend tests are in [backend/tests](backend/tests). Set `TEST_DATABASE_URL` to also run the storage tests against a real Postgres, as CI is configured to do.

## Design decisions

- **The LLM can only propose.** Even a manipulated model cannot book. Tool results and user text are treated as data.
- **Model arguments are never trusted.** Tool calls are validated with pydantic, ids must resolve to real mock inventory, and policy is re-run server-side.
- **Synchronous replies, not streaming.** Replies are short, and streaming would add transport complexity on two channels for little benefit.
- **History is deleted only by the user.** At the limit they choose which chat goes. Bookings are never deleted.
- **User slots lapse, data doesn't.** A hard first-come cap would lock reviewers out.
- **Serial queue per conversation.** A message sent while the assistant is working is handled next. The cost is that the service must run as a single instance.
- **Deterministic mock ids.** A flight id encodes route, date, option, and cabin, so it can be re-validated independently of the model.

## Known limitations and unfinished work

**Scope**

- Flights, hotels, prices, and bookings are simulated.
- No account linking between web and Telegram.
- No cancelling or interrupting a request in progress: a correction is queued behind it.
- Telegram handles text only. Edited messages are not processed.
- Reopened or reloaded chats show finished proposal cards only as text.
- On Telegram, an older card keeps saying **PROPOSED** after a newer proposal, or after a typed "yes" or "cancel". Pressing its button updates it and books nothing extra.
- Messages sent in one browser tab or device don't appear in another until it reloads or reopens the chat.

**Reliability**

- If both Telegram send attempts fail, the reply is saved but not redelivered; the user has to ask again.
- If the LLM fails right after creating a proposal, no card is shown until the user replies "yes" (on web, also after a reload).
- **Try again** sends those words as a new message. Gemini can use the history to retry; the dev fake model cannot.
- In Postgres, marking a proposal confirmed and writing the booking are not in one transaction.
- The *My trips* panel shows "no trips" if loading them fails.

**Operations and security**

- Single instance only: the queue, rate limiter, and daily LLM cap are held in process memory.
- The user limit is a demo cap, not access control: new browsers or cleared storage count as new users, so one person can fill several slots.
- No real authentication. The web session id in `localStorage` is the credential (only its hash is stored), and clearing browser storage loses access to that history.
- The Postgres connection to a remote host is encrypted but does not verify the server certificate.
- Overnight flights show no "+1 day" marker.

## What I'd improve with more time

1. Close the reliability gaps above: transactional confirm and redelivery of failed Telegram replies.
2. Link web and Telegram with a one-time code so a conversation can move between them.
3. Let users cancel or correct a request while it is running, and show per-step progress.
4. Move the queue and limits into shared storage to run several instances.
5. Add real authentication and per-user budgets.
6. Integrate real flight and hotel suppliers, and build an evaluation set for the prompt.

## AI usage

AI coding tools were used to build this project. I reviewed the code and can explain every module and decision. [docs/architecture.md](docs/architecture.md) has a one-line-per-module map and a request walkthrough to support that.
