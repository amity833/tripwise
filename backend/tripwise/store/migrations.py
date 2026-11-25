"""Plain SQL migrations, applied in order and recorded in schema_migrations. No ORM."""

MIGRATIONS: list[tuple[str, str]] = [
    (
        "001_init",
        """
CREATE TABLE users (id text PRIMARY KEY, created_at bigint NOT NULL);
CREATE TABLE conversations (id text PRIMARY KEY, user_id text NOT NULL, created_at bigint NOT NULL, outage_armed boolean NOT NULL DEFAULT false);
CREATE INDEX conversations_user_idx ON conversations (user_id, created_at);
CREATE TABLE messages (id bigserial PRIMARY KEY, conversation_id text NOT NULL, message jsonb NOT NULL, created_at bigint NOT NULL);
CREATE INDEX messages_conv_idx ON messages (conversation_id, id);
CREATE TABLE proposals (token text PRIMARY KEY, user_id text NOT NULL, conversation_id text NOT NULL, status text NOT NULL, payload jsonb NOT NULL, expires_at bigint NOT NULL, created_at bigint NOT NULL);
CREATE INDEX proposals_conv_idx ON proposals (user_id, conversation_id, status);
CREATE TABLE bookings (id text PRIMARY KEY, user_id text NOT NULL, reference text NOT NULL, proposal_token text NOT NULL UNIQUE, payload jsonb NOT NULL, created_at bigint NOT NULL);
CREATE INDEX bookings_user_idx ON bookings (user_id);
CREATE TABLE processed_events (channel text NOT NULL, event_id text NOT NULL, state text NOT NULL, result jsonb, created_at bigint NOT NULL, PRIMARY KEY (channel, event_id));
""",
    ),
    (
        "002_chat_history_and_user_slots",
        """
ALTER TABLE users ADD COLUMN last_seen_at bigint NOT NULL DEFAULT 0;
ALTER TABLE conversations ADD COLUMN active_at bigint NOT NULL DEFAULT 0;
UPDATE conversations SET active_at = created_at;
""",
    ),
    (
        "003_proposal_shown",
        """
ALTER TABLE proposals ADD COLUMN shown boolean NOT NULL DEFAULT false;
""",
    ),
]
