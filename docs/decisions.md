# Design decisions

**Telegram DMs only.** Travel planning is private and personal, and a DM is the simplest unit of isolation: one chat, one user. Groups get a polite "I only work in DMs".

**Identity and conversation are separate.** A user owns many conversations and all bookings. `/new` resets the chat without losing trips. Web identity is a random session id kept in `localStorage` and sent in a header (never the URL). Only its hash is stored.

**Five chats, deleted only by the user.** At the limit, "New chat" is refused and the user picks one to delete, so history never disappears silently. An empty chat is reused, so repeated "New chat" doesn't use up the limit. Deleting removes messages and proposals but never bookings.

**Current chat = most recently active.** Opening or writing to a chat makes it current, which Telegram needs because its messages carry no conversation id. A button on an older chat's card acts in that chat without switching to it.

**User slots expire instead of data.** At most `MAX_USERS` users seen in the last 24 hours are admitted. A hard first-come cap would lock reviewers out. Nothing is deleted when a slot lapses. The check is atomic (an advisory lock in Postgres).

**Postgres despite "in-memory is acceptable".** A sleeping free-tier service would wipe history in the middle of a review, and the brief wants history to stay visible. The in-memory store remains the default for dev and tests, and its limits are documented. Both stores pass the same contract tests, including against a real Postgres in CI.

**Booking is not an LLM tool (prompt-injection position).** Tool results and user text are data. Even a fully jailbroken model can only create *proposals*. Confirming is deterministic code that checks the owner, the conversation, expiry, and policy again, and flips the status atomically. A hallucinated `flight_id` can't resolve to inventory, so it returns a tool error.

**A typed "yes" needs a seen card.** Only explicit approval words confirm ("ok" does not). A proposal is marked as shown when Telegram accepts the card, or when the web app reports that it is on screen. A "yes" to an unseen proposal shows the card instead of booking.

**Synchronous POST plus a typing indicator, not streaming.** Replies are short and tool-driven. Streaming would add transport complexity on two channels for little benefit, and Telegram can't stream text anyway. Streaming is listed as an improvement.

**Full transcript stored, text-only shown.** Follow-ups like "book the second one" need the earlier tool results. The UI and history endpoint show only user and assistant text.

**Claim, done, release for events.** Marking an event processed *before* the work would lose the retry after a crash. Claiming first, completing after, and releasing on failure keeps duplicates out and retries in. A claim that is never completed expires after two minutes.

**Serial queue per conversation.** It makes interruptions predictable: a message sent while the assistant is working is queued and handled next. The cost is that it is per process, so the service runs as one instance.

**Deterministic ids.** A flight id encodes route, date, index, and cabin, so it can be re-derived and validated without a database. That is what lets propose and confirm re-check policy independently of the model.

**Dev fake model.** `LLM_PROVIDER=fake` is a keyword script, so a reviewer can run the full flow with no key. It is separate from the scripted fake used in tests.
