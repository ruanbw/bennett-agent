-- Bennett Agent schema，唯一事实来源。整段幂等，在一个事务里执行。
-- 改表直接改这里，然后重建开发库：docker compose down -v && docker compose up -d postgres

CREATE TABLE IF NOT EXISTS app_setting (
    key        VARCHAR(80) NOT NULL PRIMARY KEY,
    value      TEXT        NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS model_provider (
    id            UUID         NOT NULL PRIMARY KEY,
    name          VARCHAR(80)  NOT NULL,
    slug          VARCHAR(60)  NOT NULL,
    base_url      VARCHAR(500) NOT NULL,
    api_key       TEXT,
    auth_style    VARCHAR(16)  NOT NULL DEFAULT 'bearer',
    extra_headers JSONB        NOT NULL DEFAULT '{}'::jsonb,
    preset_key    VARCHAR(40),
    enabled       BOOLEAN      NOT NULL,
    created_at    TIMESTAMPTZ  NOT NULL,
    updated_at    TIMESTAMPTZ  NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_model_provider_slug ON model_provider (slug);

CREATE TABLE IF NOT EXISTS provider_model (
    id                         UUID         NOT NULL PRIMARY KEY,
    provider_id                UUID         NOT NULL REFERENCES model_provider (id) ON DELETE CASCADE,
    model_id                   VARCHAR(200) NOT NULL,
    display_name               VARCHAR(200) NOT NULL,
    description                TEXT,
    reasoning                  BOOLEAN,
    supported_reasoning_levels JSONB        NOT NULL,
    context_window             INTEGER,
    max_output_tokens          INTEGER,
    raw                        JSONB,
    enabled                    BOOLEAN      NOT NULL,
    is_default                 BOOLEAN      NOT NULL,
    sort_order                 INTEGER      NOT NULL,
    created_at                 TIMESTAMPTZ  NOT NULL,
    updated_at                 TIMESTAMPTZ  NOT NULL,
    CONSTRAINT uq_provider_model UNIQUE (provider_id, model_id)
);
CREATE INDEX IF NOT EXISTS ix_provider_model_provider_id ON provider_model (provider_id);

-- is_active = false 即封禁
CREATE TABLE IF NOT EXISTS "user" (
    role            VARCHAR(20)   NOT NULL DEFAULT 'user',
    id              UUID          NOT NULL PRIMARY KEY,
    email           VARCHAR(320)  NOT NULL,
    hashed_password VARCHAR(1024) NOT NULL,
    is_active       BOOLEAN       NOT NULL,
    is_superuser    BOOLEAN       NOT NULL,
    is_verified     BOOLEAN       NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_user_email ON "user" (email);

-- 服务端会话：一条记录 = 一个登录态，登出/踢人即删行
CREATE TABLE IF NOT EXISTS accesstoken (
    user_id    UUID        NOT NULL REFERENCES "user" (id) ON DELETE CASCADE,
    token      VARCHAR(43) NOT NULL PRIMARY KEY,
    created_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_accesstoken_created_at ON accesstoken (created_at);

CREATE TABLE IF NOT EXISTS sync_counter (
    user_id UUID   NOT NULL PRIMARY KEY REFERENCES "user" (id) ON DELETE CASCADE,
    version BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS conversation (
    id               VARCHAR(64) NOT NULL PRIMARY KEY,
    user_id          UUID        NOT NULL REFERENCES "user" (id) ON DELETE CASCADE,
    title            TEXT        NOT NULL,
    model            VARCHAR(255),
    reasoning_effort VARCHAR(32),
    is_pinned        BOOLEAN     NOT NULL,
    deleted          BOOLEAN     NOT NULL,
    client_rev       INTEGER     NOT NULL,
    seq_counter      INTEGER     NOT NULL,
    version          BIGINT      NOT NULL,
    created_at       BIGINT      NOT NULL,
    updated_at       BIGINT      NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_conversation_pull ON conversation (user_id, version);

CREATE TABLE IF NOT EXISTS message (
    id              VARCHAR(64) NOT NULL PRIMARY KEY,
    conversation_id VARCHAR(64) NOT NULL REFERENCES conversation (id) ON DELETE CASCADE,
    user_id         UUID        NOT NULL REFERENCES "user" (id) ON DELETE CASCADE,
    role            VARCHAR(16) NOT NULL,
    payload         JSONB       NOT NULL,
    client_rev      INTEGER     NOT NULL,
    seq             INTEGER     NOT NULL,
    version         BIGINT      NOT NULL,
    created_at      BIGINT      NOT NULL,
    CONSTRAINT uq_message_conversation_seq UNIQUE (conversation_id, seq)
);
CREATE INDEX IF NOT EXISTS ix_message_conversation ON message (conversation_id, seq);
CREATE INDEX IF NOT EXISTS ix_message_pull ON message (user_id, version);

CREATE TABLE IF NOT EXISTS knowledge_document (
    id              UUID         NOT NULL PRIMARY KEY,
    user_id         UUID         NOT NULL REFERENCES "user" (id) ON DELETE CASCADE,
    title           VARCHAR(200) NOT NULL,
    source          VARCHAR(500),
    embedding_model VARCHAR(200) NOT NULL,
    dimensions      INTEGER      NOT NULL,
    chunk_count     INTEGER      NOT NULL,
    created_at      TIMESTAMPTZ  NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_knowledge_document_user ON knowledge_document (user_id, created_at);

CREATE TABLE IF NOT EXISTS knowledge_chunk (
    id          UUID         NOT NULL PRIMARY KEY,
    document_id UUID         NOT NULL REFERENCES knowledge_document (id) ON DELETE CASCADE,
    user_id     UUID         NOT NULL REFERENCES "user" (id) ON DELETE CASCADE,
    chunk_index INTEGER      NOT NULL,
    heading     VARCHAR(200) NOT NULL,
    text        TEXT         NOT NULL,
    embedding   BYTEA        NOT NULL,
    dimensions  INTEGER      NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_knowledge_chunk_document ON knowledge_chunk (document_id, chunk_index);
CREATE INDEX IF NOT EXISTS ix_knowledge_chunk_user ON knowledge_chunk (user_id);

CREATE TABLE IF NOT EXISTS uploaded_file (
    id                    UUID         NOT NULL PRIMARY KEY,
    user_id               UUID         NOT NULL REFERENCES "user" (id) ON DELETE CASCADE,
    original_name         VARCHAR(255) NOT NULL,
    content_type          VARCHAR(128) NOT NULL,
    size_bytes            BIGINT       NOT NULL,
    sha256                VARCHAR(64)  NOT NULL,
    storage_key           VARCHAR(512) NOT NULL,
    status                VARCHAR(16)  NOT NULL,
    attempts              INTEGER      NOT NULL,
    error                 TEXT,
    processing_started_at TIMESTAMPTZ,
    document_id           UUID         REFERENCES knowledge_document (id) ON DELETE SET NULL,
    created_at            TIMESTAMPTZ  NOT NULL,
    updated_at            TIMESTAMPTZ  NOT NULL,
    CONSTRAINT uq_uploaded_file_user_sha256 UNIQUE (user_id, sha256)
);
CREATE INDEX IF NOT EXISTS ix_uploaded_file_queue ON uploaded_file (status, created_at);
CREATE INDEX IF NOT EXISTS ix_uploaded_file_user ON uploaded_file (user_id, created_at);

CREATE TABLE IF NOT EXISTS sandbox_lease (
    id               VARCHAR(64)  NOT NULL PRIMARY KEY,
    user_id          UUID         NOT NULL REFERENCES "user" (id) ON DELETE CASCADE,
    session_id       VARCHAR(64)  NOT NULL,
    provider         VARCHAR(32)  NOT NULL,
    provider_ref     VARCHAR(255),
    template         VARCHAR(128) NOT NULL,
    template_version VARCHAR(64)  NOT NULL,
    state            VARCHAR(16)  NOT NULL,
    created_at       TIMESTAMPTZ  NOT NULL,
    last_activity    TIMESTAMPTZ,
    hard_expires_at  TIMESTAMPTZ  NOT NULL,
    destroyed_at     TIMESTAMPTZ,
    destroy_reason   VARCHAR(32),
    artifact_uri     VARCHAR(512),
    error            TEXT
);
-- 每用户一台；谓词须与 app/db.py:SandboxLease 一致
CREATE UNIQUE INDEX IF NOT EXISTS ux_sandbox_lease_one_per_user
    ON sandbox_lease (user_id)
    WHERE state IN ('provisioning', 'ready', 'paused');
CREATE INDEX IF NOT EXISTS ix_sandbox_lease_reap ON sandbox_lease (state, hard_expires_at);

CREATE TABLE IF NOT EXISTS sandbox_command (
    id           BIGSERIAL   NOT NULL PRIMARY KEY,
    lease_id     VARCHAR(64) NOT NULL REFERENCES sandbox_lease (id) ON DELETE CASCADE,
    seq          INTEGER     NOT NULL,
    command      TEXT        NOT NULL,
    exit_code    INTEGER,
    duration_ms  INTEGER,
    output_bytes BIGINT,
    output_head  TEXT,
    truncated    BOOLEAN     NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_sandbox_command_lease ON sandbox_command (lease_id, seq);
