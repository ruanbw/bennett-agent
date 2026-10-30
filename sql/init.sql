-- Bennett Agent schema，唯一事实来源。整段幂等，在一个事务里执行。
-- 改表直接改这里，然后重建开发库：docker compose down -v && docker compose up -d postgres
--
-- 枚举列一律带 CHECK：取值必须与 Python 常量逐字一致（state 见 services/sandbox/lease.py，
-- role 见 app/roles.py，auth_style 见 services/model_service/presets.py）。
-- 线上补加约束必须分两步：先 ADD CONSTRAINT ... NOT VALID（只拿短暂的锁，不扫表），
-- 再 VALIDATE CONSTRAINT（SHARE UPDATE EXCLUSIVE，不阻塞读写）；直接 ADD 会 ACCESS EXCLUSIVE 扫全表。

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
    updated_at    TIMESTAMPTZ  NOT NULL,
    CONSTRAINT ck_model_provider_auth_style CHECK (auth_style IN ('bearer', 'none', 'x-api-key', 'x-goog-api-key', 'api-key'))
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
-- 不再单独建 ix_provider_model_provider_id：上面前导列已覆盖，同列索引是纯写放大。
-- （存量库可能还留着这条索引，DROP INDEX 随时可做，不影响正确性。）

-- is_active = false 即封禁
CREATE TABLE IF NOT EXISTS "user" (
    role            VARCHAR(20)   NOT NULL DEFAULT 'user',
    id              UUID          NOT NULL PRIMARY KEY,
    email           VARCHAR(320)  NOT NULL,
    hashed_password VARCHAR(1024) NOT NULL,
    is_active       BOOLEAN       NOT NULL,
    is_superuser    BOOLEAN       NOT NULL,
    is_verified     BOOLEAN       NOT NULL,
    CONSTRAINT ck_user_role CHECK (role IN ('user', 'admin', 'superadmin'))
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
    updated_at       BIGINT      NOT NULL,
    CONSTRAINT uq_conversation_id_user UNIQUE (id, user_id)
);
CREATE INDEX IF NOT EXISTS ix_conversation_pull ON conversation (user_id, version);

CREATE TABLE IF NOT EXISTS message (
    id              VARCHAR(64) NOT NULL PRIMARY KEY,
    conversation_id VARCHAR(64) NOT NULL,
    user_id         UUID        NOT NULL,
    role            VARCHAR(16) NOT NULL,
    payload         JSONB       NOT NULL,
    client_rev      INTEGER     NOT NULL,
    seq             INTEGER     NOT NULL,
    version         BIGINT      NOT NULL,
    created_at      BIGINT      NOT NULL,
    CONSTRAINT uq_message_conversation_seq UNIQUE (conversation_id, seq),
    -- 冗余的 user_id 由复合外键绑死到会话归属（而不是两条互相独立的 FK）：
    -- 漏传一次 user_id，pull 就会把别人的消息投给错误租户。删 user → 删 conversation → 删 message。
    CONSTRAINT fk_message_conversation_user FOREIGN KEY (conversation_id, user_id)
        REFERENCES conversation (id, user_id) ON DELETE CASCADE,
    CONSTRAINT ck_message_role CHECK (role IN ('user', 'assistant'))
);
-- 不再建 ix_message_conversation：uq_message_conversation_seq 已给出同列的 btree。
CREATE INDEX IF NOT EXISTS ix_message_pull ON message (user_id, version);

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
    -- 进入 destroying 的时刻。没有它就无法判断「这次销毁是还在进行、还是被中断了」，
    -- 而这两者的正确处置完全相反（等 vs 立刻接手重试）。
    destroying_since TIMESTAMPTZ,
    destroy_reason   VARCHAR(32),
    artifact_uri     VARCHAR(512),
    error            TEXT,
    -- 取值见 services/sandbox/lease.py 的 STATE_*：拼错会让下面的部分唯一索引谓词不匹配。
    CONSTRAINT ck_sandbox_lease_state CHECK (
        state IN ('provisioning', 'ready', 'paused', 'destroying', 'destroyed', 'failed', 'destroy_failed')
    )
);
-- 每用户一台；谓词须与 app/db/models/sandbox.py:SandboxLease 一致
CREATE UNIQUE INDEX IF NOT EXISTS ux_sandbox_lease_one_per_user
    ON sandbox_lease (user_id)
    WHERE state IN ('provisioning', 'ready', 'paused');
CREATE INDEX IF NOT EXISTS ix_sandbox_lease_reap ON sandbox_lease (state, hard_expires_at);
-- 用户端点 get_latest_lease 的唯一形状：WHERE user_id=? ORDER BY created_at DESC LIMIT 1。
-- 它查全部状态（界面要说清「环境已回收」），只覆盖活动状态的部分唯一索引替代不了。
CREATE INDEX IF NOT EXISTS ix_sandbox_lease_user_recent ON sandbox_lease (user_id, created_at DESC);

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
