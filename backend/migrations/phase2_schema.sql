-- ============================================================
-- SenseCritiq Phase 2 Schema Migration
-- Run against Railway PostgreSQL after the Phase 1 schema.
-- ============================================================

-- ── projects ─────────────────────────────────────────────────
-- Optional grouping layer for sessions (e.g. "Q1 Onboarding Study")
CREATE TABLE IF NOT EXISTS projects (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id  UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    name        TEXT NOT NULL,
    description TEXT,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_projects_account ON projects(account_id);

-- ── quotes ───────────────────────────────────────────────────
-- Individual verbatim quotes extracted during synthesis.
-- Stored separately so they can be searched, cited, and embedded.
CREATE TABLE IF NOT EXISTS quotes (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    session_id      UUID NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    account_id      UUID NOT NULL,  -- denormalised for fast filtering
    text            TEXT NOT NULL,
    speaker         TEXT,           -- speaker label from diarisation
    timestamp_sec   INT,            -- seconds into the recording
    theme_label     TEXT,
    embedding       vector(1536),   -- text-embedding-3-small
    embedding_model TEXT DEFAULT 'text-embedding-3-small',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_quotes_session   ON quotes(session_id);
CREATE INDEX IF NOT EXISTS idx_quotes_account   ON quotes(account_id);
-- pgvector IVFFlat index for approximate nearest-neighbour search
CREATE INDEX IF NOT EXISTS idx_quotes_embedding ON quotes USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

-- ── conversations ────────────────────────────────────────────
-- One conversation = one continuous chat thread in the web UI.
CREATE TABLE IF NOT EXISTS conversations (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    account_id  UUID NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
    title       TEXT,               -- auto-generated from first message
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_conversations_account ON conversations(account_id);

-- ── messages ─────────────────────────────────────────────────
-- Every turn in a conversation: user, assistant, or tool result.
CREATE TABLE IF NOT EXISTS messages (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL CHECK (role IN ('user', 'assistant', 'tool')),
    content         TEXT NOT NULL,
    tool_name       TEXT,           -- populated for role='tool'
    tool_input      JSONB,          -- the tool call arguments
    tool_result     JSONB,          -- the tool response
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_messages_conversation ON messages(conversation_id);
CREATE INDEX IF NOT EXISTS idx_messages_created      ON messages(conversation_id, created_at);

-- ── updated_at trigger for conversations ─────────────────────
CREATE OR REPLACE FUNCTION update_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS conversations_updated_at ON conversations;
CREATE TRIGGER conversations_updated_at
    BEFORE UPDATE ON conversations
    FOR EACH ROW EXECUTE FUNCTION update_updated_at();

-- ── session_id column on conversations (optional) ────────────
-- Allows a conversation to be anchored to a specific session
-- (e.g. "Tell me more about this interview").
ALTER TABLE conversations ADD COLUMN IF NOT EXISTS session_id UUID REFERENCES sessions(id);
