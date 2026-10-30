---
title: 配置项参考
description: Settings 里 89 个字段的变量名、类型、默认值与作用，附启动期校验与最小可运行配置。
---

# 配置项参考

这一页是查表用的。`app/config/settings.py` 的 `Settings` 共 89 个字段，一个字段对应一个环境变量；表里给出变量名、声明类型、代码里的默认值与它控制什么。讲原理与加载顺序的是 [配置手册](../guide/configuration.md)。

## 变量名怎么来的

- `model_config` 只设了 `env_file`、`env_file_encoding="utf-8"`、`extra="ignore"`、`hide_input_in_errors=True`（`app/config/settings.py:871`）。**没有 `env_prefix`**，变量名就是字段名本身，不存在额外前缀；也没设 `case_sensitive`，pydantic-settings 默认不区分大小写，所以 `DATABASE_URL`、`database_url`、`Database_Url` 指向同一个字段（约定写成全大写）。
- `extra="ignore"` 意味着**拼错的变量名不会报错**，只会静默不生效。
- 所有字段都有默认值，没有必填项。`settings = Settings()` 在导入 `app.config` 时构造一次（`app/config/settings.py:881`），所以配置只在进程启动时读一次。
- dotenv 文件的加载顺序、`APP_ENV` 如何决定加载哪个文件，见 [配置手册](../guide/configuration.md)。

表里「可空」表示声明类型是 `X | None`，不写这一项时取 `None`。「敏感」表示该值不应进版本库、不进镜像，生产由密钥管理注入。

还有一个易踩的差异：pydantic-settings 默认不忽略空值，`X=` 得到的是空字符串而不是 `None`。对 `str | None` 字段两者大多等效，但对字面量字段是错误——`AUTH_COOKIE_SAMESITE=` 会拿空串去匹配 `Literal["lax","strict","none"]` 并启动失败。想让这类字段走「自动」语义，就别写这一行。

## 服务与环境

| 变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `APP_ENV` | str | `development` | 只接受 `development` / `production` / `staging` / `test`，首尾空格与大小写会被归一化，其余取值直接报错。决定额外加载哪个 `.env.{env}`，也决定下面所有生产守卫是否生效。 |
| `ENABLE_API_DOCS` | bool（可空） | `None` | 是否暴露 `/docs`、`/redoc`、`/openapi.json`。`None` = 自动：production 关闭、其余环境开启（`app/server.py:205`）。 |
| `ASSISTANT_NAME` | str | `Bennett` | 展示名。进 OpenAPI 标题（`app/server.py:330`）、系统提示词与首轮标题生成，同时是 agent 装配缓存键的一部分。 |
| `CORS_ALLOWED_ORIGINS` | str | `""` | 逗号分隔的来源白名单。留空时 development 回退 `localhost:5173/5174` 与 `127.0.0.1:5173/5174`，production 不回退（`app/config/settings.py:844`）。尾斜杠、重复项、只剩协议的坏值会被清掉，`*` 保留。 |
| `CORS_ALLOW_CREDENTIALS` | bool | `False` | 允许浏览器携带 cookie。设为 true 后生效的 SameSite 会变成 `none`（见 `AUTH_COOKIE_SAMESITE`），且来源列表不能含 `*`。 |
| `MAX_REQUEST_BODY_BYTES` | int | `33554432` | 单个请求体的硬上限（字节，下限 1024），默认 32 MiB。超限返回 413 且不会把 body 读进内存（`app/middleware.py:45`）。 |

同一组里还有一条非 Settings 的约束：来源白名单同时供 `OriginGuardMiddleware` 使用（`app/server.py:358`），它拦的是 `/api/*` 的非安全方法——浏览器发起的跨站 POST 会被拒成 403。

## 模型网关

| 变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `LITELLM_API_BASE` | str | `http://localhost:4000` | 首次启动引导出的「默认网关」提供商的 base_url（`app/services/model_service/provider_sync.py:512`）；库里没有可用提供商时，它仍是兜底端点（`app/services/agent_factory.py:158`）。 |
| `LITELLM_API_KEY` | str（可空） | `None` | 敏感。同上，只在引导时写进库（加密存储），运行期读库。兜底路径下为空会用 `sk-no-key` 占位发起调用。 |
| `MODEL_NAME` | str | `openai/gpt-4o` | 引导时写入的模型标识（LiteLLM 路由格式），即默认模型。引导后模型清单以数据库为准。 |
| `FALLBACK_MODEL_NAME` | str（可空） | `None` | 备用模型。主模型 5xx / 429 / 超时 / 连接失败时切换一次；401、403、400、404 不切换。留空 = 不启用。 |
| `MODEL_DESCRIPTION_FIELD` | str | `magpie_label` | 上游模型条目里用作展示描述的字段名，缺失时回退为 `provider name`（`app/services/model_service/gateway.py:215`）。 |
| `MODEL_UPSTREAM_TIMEOUT` | float | `120.0` | 上游调用的**空闲**超时（秒，≥0）：多久没收到数据就放弃。0 = 不限制。不是总时长，所以不会提前掐断长流式输出。 |
| `PROVIDER_BLOCK_PRIVATE_TARGETS` | bool | `False` | 提供商出站目标是否必须是公网地址。无论开关如何，链路本地、未指定、组播地址（含云元数据服务）永远拒绝（`app/services/model_service/outbound_guard.py:131`）。 |
| `BOOTSTRAP_PROVIDER_FROM_ENV` | bool | `True` | 全新库首次启动时是否用上面三项引导一条提供商。初始化标记写入后永不清除，删光提供商重启也不会再次引导（`app/services/model_service/provider_sync.py:509`）。 |

## 工具与外部依赖

| 变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `TAVILY_API_KEY` | str（可空） | `None` | 敏感。联网搜索工具的 Key。空白或命中占位词（`your-api-key`、`change-me`、`placeholder` 之类）时视为未配置：工具不装配、能力探针不报可联网（`app/config/settings.py:43`）。 |
| `MCP_SERVERS` | str | `""` | 分号分隔的 MCP server 声明，每条形如 `名称=url:https://host/mcp` 或 `名称=stdio:/path/to/server arg1`。名称只能字母/数字/下划线/短横线、长度 ≤32、不可重复（`app/tools/mcp_client.py:122`）。 |
| `MCP_TOOL_TIMEOUT_SECONDS` | float | `30.0` | 单个 MCP 工具调用的默认超时（秒，1–600）。声明语法里没有按 server 覆盖超时的写法（`McpServerConfig.timeout` 恒为 `None`），所以它就是所有 MCP 工具调用的实际超时（`app/tools/mcp_client.py:83`）。 |

`MCP_SERVERS` 写错的后果与别的配置不同：`load_server_configs()` 抛出的 `ValueError` 没有任何兜底，启动会失败（`app/tools/mcp_client.py:140` → `app/server.py:177`）。工具是启动时动态发现的，server 连不上只打日志、不影响启动。

## 上下文窗口预算

| 变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `CONTEXT_GUARD_MODE` | `Literal["off","reject","compact"]` | `compact` | 历史超出模型上下文窗口时的处置：`compact` 先压缩、压完仍超才拒绝；`reject` 直接拒绝本轮；`off` 不判定。网关没报 `context_window` 的模型一律放行。 |
| `CONTEXT_RESERVE_TOKENS` | int | `8192` | 预留给模型写回复的 token（≥0）。网关报了 `max_output_tokens` 就用上游值；两种取值都会被压在窗口的一半以内（`app/services/token_budget.py:108`）。 |
| `CONTEXT_SAFETY_RATIO` | float | `0.9` | 安全系数（0–1，不含 0）：可用上限 =（窗口 − 预留 − 固定开销）× 本值（`app/services/token_budget.py:158`）。token 数是估算值，留一成余量避免贴着上限判断。 |
| `CONTEXT_COMPACT_KEEP_RECENT_TURNS` | int | `4` | 压缩时保留最近多少**轮**问答原文（≥1，一轮 = user + assistant 两条），更早的折叠成摘要。 |
| `CONTEXT_COMPACT_MESSAGE_MAX_CHARS` | int | `4000` | 压缩第一级（截断超长单条）的单条字符上限（≥500）。只作用于本轮问题之外的旧消息。 |

五个字段全部取自 `app/config/settings.py:354` 起的连续区块；实际计算在 `app/services/token_budget.py` 与 `app/services/context_compaction.py`，两者都不依赖分词器。

## 数据库

| 变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `DATABASE_URL` | str | `postgresql+asyncpg://bennett:bennett@127.0.0.1:5432/bennett` | SQLAlchemy 异步连接串，只接受 `postgresql+asyncpg://` 前缀（`app/config/settings.py:738`）。密码含特殊字符时必须 URL 编码。 |
| `DATABASE_POOL_SIZE` | int | `10` | 常驻连接数（≥1）。**总连接数 =（本值 + `DATABASE_MAX_OVERFLOW`）× worker 数**。 |
| `DATABASE_MAX_OVERFLOW` | int | `10` | 池满后允许临时新建的连接数（≥0），超出后等待 `DATABASE_POOL_TIMEOUT` 再报错。 |
| `DATABASE_CONNECT_TIMEOUT` | float | `5.0` | 建连超时（秒，>0）。asyncpg 自身的默认值是 60 秒，数据库不可达时挂一分钟才报一个看不出原因的错。 |
| `DATABASE_POOL_TIMEOUT` | float | `10.0` | 等空闲连接的最长秒数（>0），必须明显小于客户端与反向代理的超时。 |
| `DATABASE_POOL_RECYCLE` | int | `1800` | 连接最长存活秒数（≥0，0 = 不回收），必须短于 Postgres / pgbouncer / 云负载均衡的空闲断连时间。 |
| `DATABASE_STATEMENT_TIMEOUT_MS` | int | `30000` | 服务端语句超时（毫秒，≥0，0 = 不限制），随连接下发（`app/db/base.py:79`）。应用层超时只放弃等待，不会取消库里的查询。 |
| `DATABASE_POOL_MODE` | `Literal["pool","null"]` | `pool` | `pool` = 常驻连接池；`null` = 每次取用新建连接。`null` 用于短命脚本与跨事件循环的同步 `TestClient`（`app/db/base.py:82`）。 |
| `DATABASE_APPLY_SCHEMA` | bool | `True` | 启动时执行 `sql/init.sql` 并做逐列漂移比对。设 false 会**整段跳过**，包括漂移检测（`app/db/schema.py:26`）。 |

这一组之外还有一条不在 Settings 里的约束：`APP_ENV=production` 时不要在命令行加 `--reload`，`main.py` 会直接 `parser.error` 退出。

## Redis

| 变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `REDIS_URL` | str | `redis://127.0.0.1:6379/0` | 连接串。Redis 不是可用性单点：限流、会话缓存、搜索缓存都有降级路径。 |
| `REDIS_KEY_PREFIX` | str | `bennett` | 所有键的公共前缀（限流键、会话缓存、搜索缓存）。多套部署共用一个实例时必须各不相同。 |
| `REDIS_CONNECT_TIMEOUT` | float | `2.0` | 建连超时（秒，>0），必须短。 |
| `REDIS_SOCKET_TIMEOUT` | float | `1.0` | 单次命令读写超时（秒，>0）。 |
| `REDIS_MAX_CONNECTIONS` | int | `50` | 连接池上限（≥1）。总连接数 = 本值 × 进程数，需小于 Redis 的 `maxclients`。 |
| `REDIS_SESSION_CACHE_ENABLED` | bool | `True` | 是否用 Redis 加速会话 token → 用户的解析。数据库仍是权威来源，删会话时两边都删（`app/db/session_cache.py:28`）。 |
| `REDIS_SESSION_CACHE_TTL_SECONDS` | int | `60` | 会话缓存最长存活秒数（≥1），且不会超过会话剩余寿命（`app/db/session_cache.py:100`）。 |
| `SEARCH_CACHE_BACKEND` | `Literal["redis","memory"]` | `redis` | Tavily 搜索缓存的存放位置；`memory` = 进程内 LRU，是 Redis 不可用时的降级形态（`app/tools/tavily_cache.py:106`）。 |

Redis 故障时的行为由限流那两个字段决定（见下一节），缓存类调用则是直接 miss 并累加 `redis_unavailable_total`（`app/services/redis_client.py:66`）。

## 对象存储

| 变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `S3_ENDPOINT_URL` | str | `""` | S3 API 地址，如 `http://127.0.0.1:9000`。留空则用 boto3 的默认 AWS 端点，只在真的用 AWS S3 时成立。 |
| `S3_REGION` | str | `us-east-1` | 签名 region。与桶实际 region 不一致会被拒为 `AuthorizationHeaderMalformed`。 |
| `S3_ACCESS_KEY` | str | `""` | 敏感。与 secret 成对出现，必须与存储服务容器里的一致。 |
| `S3_SECRET_KEY` | str | `""` | 敏感。生产由密钥管理注入，不写进镜像或 compose 文件。 |
| `S3_BUCKET` | str | `bennett-artifacts` | 沙箱产物桶名。必须匹配 `^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$`，否则启动失败（`app/config/settings.py:752`）。 |
| `S3_PREFIX` | str | `sandbox` | 桶内键前缀。键名只用固定前缀 + 服务端生成的 ID 拼接，不拼用户提供的文件名。 |
| `S3_USE_PATH_STYLE` | bool | `True` | path-style 寻址（`endpoint/bucket/key`）。RustFS / MinIO 等自建网关基本只支持这种；AWS 官方 S3 建议关掉。 |
| `S3_CREATE_BUCKET` | bool | `True` | 沙箱启用时启动确保桶存在（幂等）。生产建议关掉，改由 IaC 建桶并只授予最小权限。 |
| `S3_PRESIGN_EXPIRY_SECONDS` | int | `300` | 产物下载预签名 URL 有效期（秒，30–3600）。 |
| `S3_CONNECT_TIMEOUT` | float | `3.0` | 建连超时（秒，>0）。 |
| `S3_READ_TIMEOUT` | float | `60.0` | 读写超时（秒，>0），比对话上游宽松：上传几十 MB 是正常操作。 |

对象存储只服务沙箱产物导出。`SANDBOX_ENABLED=false` 时整段配置可以留空，启动也完全跳过对象存储检查（`app/server.py:162`）。

## 认证与会话

| 变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `AUTH_SECRET` | str | 内置占位常量 `DEFAULT_AUTH_SECRET`（`app/config/settings.py:18`） | 敏感。重置密码/邮箱验证令牌的签名密钥，同时经 SHA-256 派生出加密提供商 API Key 的密钥。更换后已加密的 Key 解不开，需在后台重填。生产要求 ≥32 字符且不含占位词。 |
| `AUTH_COOKIE_NAME` | str | `bennett_session` | 会话 cookie 名。只允许 RFC 6265 token 字符，不能含空格、分号、逗号或中文（`app/config/settings.py:798`）。改名等于让所有已登录用户掉线。 |
| `AUTH_COOKIE_SECURE` | bool | `False` | cookie 是否只走 HTTPS。本地 http 调试必须保持 false，否则浏览器不回传 cookie，表现为「登录成功但下一个请求未登录」。 |
| `AUTH_COOKIE_SAMESITE` | `Literal["lax","strict","none"]`（可空） | `None` | `None` = 自动：`CORS_ALLOW_CREDENTIALS=true` 时取 `none`，否则取 `lax`（`app/config/settings.py:821`）。生效值为 `none` 时必须 `AUTH_COOKIE_SECURE=true`。 |
| `SESSION_LIFETIME_SECONDS` | int | `2592000` | 登录态有效期（秒，≥60，默认 30 天），同时决定 cookie 的 Max-Age（`app/services/auth.py:71`）。 |
| `REGISTRATION_ENABLED` | bool | `True` | 是否允许公开注册。关闭后注册端点返回 403（`app/api/auth.py:77`），需要用 `scripts/seed_admin.py` 或后台建号。 |

## 限流与可信代理

| 变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `CHAT_RATE_LIMIT_PER_MINUTE` | int | `30` | 单个**账号**每分钟的对话/标题请求数（≥0，0 = 不限流）。 |
| `AUTH_RATE_LIMIT_PER_MINUTE` | int | `10` | 单个**来源 IP** 每分钟的登录 / 注册请求数（≥0）。登录与注册各自一个桶（桶名取路径末段，`app/api/rate_limit.py:401`）。 |
| `CHANGE_PASSWORD_RATE_LIMIT_PER_MINUTE` | int | `5` | 单个**账号**每分钟的改密请求数（≥0）。每次校验当前密码都是一次 Argon2 计算（`app/api/auth.py:147`）。 |
| `RATE_LIMIT_BACKEND` | `Literal["redis","memory"]` | `redis` | 计数存储。`memory` = 进程内（单 worker 够用）；配额变化时会重建限流器（`app/api/rate_limit.py:324`）。 |
| `RATE_LIMIT_ON_REDIS_ERROR` | `Literal["local_fallback","fail_closed","fail_open"]` | `local_fallback` | Redis 不可用时的处置：退到进程内计数 / 一律 429 / 一律放行（`app/api/rate_limit.py:290`）。三种都会告警并累加 `redis_unavailable_total`。 |
| `TRUSTED_PROXY_HEADERS` | str | `""` | 受信任的代理 IP 标头，如 `X-Forwarded-For,X-Real-IP`。只有直连对端落在 `TRUSTED_PROXY_IPS` 内才采信。 |
| `TRUSTED_PROXY_IPS` | str | `""` | 受信代理的 IP / CIDR（逗号分隔）。解析从右往左跳过受信代理，取第一个非代理地址作为客户端 IP（`app/api/rate_limit.py:160`）。非法条目忽略并告警。 |

`TRUSTED_PROXY_HEADERS` 配了但 `TRUSTED_PROXY_IPS` 为空时，标头一概不采信，启动日志会点名这一项（`app/server.py:49`）。触发限流时返回 429，带 `Retry-After` 与 `X-RateLimit-*` 响应头（`app/api/rate_limit.py:356`）。

## 沙箱

| 变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `SANDBOX_ENABLED` | bool | `False` | 总开关。关闭时 `capabilities.sandbox=false`，四个沙箱工具（exec / write_file / read_file / reset）不装配，`/api/sandbox/*` 返回 404，`/health/ready` 里的存储探测也跳过。 |
| `SANDBOX_PROVIDER` | `Literal["opensandbox"]` | `opensandbox` | 沙箱后端，目前只有这一个取值。 |
| `SANDBOX_TEMPLATE` | str | `bennett-sandbox` | 模板 / 镜像名。 |
| `SANDBOX_TEMPLATE_VERSION` | str | `0.1` | 模板版本，落库到租约上，便于追溯用户当时跑在哪一版（`app/services/sandbox/lease.py:257`）。 |
| `SANDBOX_CPU` | float | `2.0` | 每台机器的 vCPU（>0）。 |
| `SANDBOX_MEMORY_MB` | int | `4096` | 每台机器的内存 MB（≥256）。 |
| `SANDBOX_WORKDIR` | str | `/home/sandbox/workspace` | 沙箱内的工作目录。 |
| `SANDBOX_IDLE_SECONDS` | int | `600` | 空闲回收阈值（≥60）：`max(心跳, 最近活动)` 超过它就销毁。同时是心跳键的 Redis TTL（`app/services/sandbox/lease.py:520`）。 |
| `SANDBOX_HARD_TTL_SECONDS` | int | `14400` | 绝对上限（≥300，默认 4 小时）。到点无论有没有心跳都销毁，产物仍会先导出。 |
| `SANDBOX_HEARTBEAT_SECONDS` | int | `30` | 前端心跳间隔（≥5）。**后端不读这个值**：心跳端点只刷新 TTL，前端周期是 `frontend/src/lib/sandbox.ts:20` 里的常量。 |
| `SANDBOX_MAX_PER_USER` | int | `1` | 每个用户同时活着的机器数（≥1）。默认 1 = 跨会话共用同一台。 |
| `SANDBOX_MAX_CONCURRENT` | int | `12` | 全局并发上限（≥1），节点池容量保护。 |
| `SANDBOX_EGRESS` | `Literal["proxy","open"]` | `proxy` | 出站策略。**后端不读这个值**：当前没有消费它的代码，实际出站由 OpenSandbox server 侧配置（`docker/opensandbox.toml` 的 `[egress]`）决定。 |
| `SANDBOX_ARTIFACT_MAX_BYTES` | int | `209715200` | 销毁前导出产物的体积上限（默认 200 MiB）。超限（或体积量不出来）只导文件清单，不打包归档（`app/services/sandbox/artifacts.py:44`）。 |
| `SANDBOX_SERVER_URL` | str（可空） | `http://127.0.0.1:8080` | OpenSandbox server 地址。不可达时工具回「执行环境暂时不可用，请稍后再试。」（`app/tools/sandbox.py:40`），不影响对话其余部分。 |
| `SANDBOX_SERVER_API_KEY` | str（可空） | `None` | 敏感。server 的 API key。为空时 OpenSandbox server 自身要求显式确认无鉴权（`OPENSANDBOX_INSECURE_SERVER=YES`），生产必须配。 |

容量超限的行为与 HTTP 无关：建机路径抛 `SandboxCapacityError`（`app/services/sandbox/lease.py:240`、`:249`），工具层把它翻成一句给模型看的普通工具结果（`app/tools/sandbox.py:166`），不是 HTTP 429。`/api/sandbox/*` 在总开关关闭时返回 404。

## 跨设备同步

| 变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `SYNC_RATE_LIMIT_PER_MINUTE` | int | `120` | 单个账号每分钟的同步请求数（≥0），push 与 pull 共用一个桶，与对话配额分开（`app/api/sync.py:28`）。 |
| `SYNC_PUSH_MAX_ROWS` | int | `200` | 单次 push 最多携带的行数（1–2000，会话 + 消息合计），超出整批拒绝并返回 400（`code=too_many_rows`，`app/api/sync.py:58`）。 |
| `SYNC_PULL_MAX_ROWS` | int | `500` | 单次 pull **每个数据流**最多返回的行数（1–5000）。两路都拉完游标才前进。 |
| `SYNC_PULL_MAX_BYTES` | int | `8388608` | 单次 pull 响应里消息 payload 的累计上限（≥1024，默认 8 MiB）。只卡行数挡不住大响应。 |
| `SYNC_PAYLOAD_MAX_BYTES` | int | `262144` | 单条消息 payload 序列化后的上限（≥1024，默认 256 KiB），超限的行按行拒绝、不影响同批其余行（`app/services/sync_service.py:526`）。 |
| `SYNC_MAX_CONVERSATIONS_PER_USER` | int | `2000` | 单用户最多保留的未删除会话行数（≥1）。 |
| `SYNC_MAX_MESSAGES_PER_CONVERSATION` | int | `5000` | 单个会话最多保留的消息行数（≥1），超出的新行按 `quota_exceeded` 拒绝。 |
| `SYNC_LONG_POLL_SECONDS` | float | `25.0` | pull 无变更时的挂起上限（0–120，0 = 立即返回）（`app/api/sync.py:97`）。 |

长轮询的唤醒是进程内的：多 worker 部署下跨进程唤醒不成立，见 [运维总览](../ops/index.md)；同步协议本身见 [跨设备同步](../features/sync.md)。

## 运维与可观测性

| 变量 | 类型 | 默认值 | 说明 |
|---|---|---|---|
| `METRICS_ENABLED` | bool | `False` | 是否暴露 `/api/metrics`（Prometheus 文本格式）。关闭时返回 **404** 而非 403（`app/api/ops.py:140`），该端点不需要登录。 |
| `READINESS_REQUIRED_DEPENDENCIES` | str | `database` | `/health/ready` 里必须可用的依赖，逗号分隔。代码认四个名字：`database`、`redis`、`storage`、`provider_keys`（`app/api/ops.py:48`）；未知名字忽略并告警，不会让启动失败。 |

必需依赖失败返回 503，非必需失败只把整体状态降级为 `degraded` 并仍返回 200（`app/api/ops.py:90`）。

## 启动期校验

配置的合法性检查全部发生在「开始服务之前」：`settings = Settings()` 在导入时构造，`create_app()` 在导入 `app.server` 时执行，两者都在 lifespan 之前。校验失败的表现是进程带着 traceback 退出，不存在「先起来、请求时再 500」的路径。

| 条件 | 结果 | 依据 |
|---|---|---|
| `APP_ENV` 不在白名单 | pydantic 校验失败（归一化后仍不认识就报错，提示合法取值） | `app/config/settings.py:764` |
| 任一字段越出 `ge` / `le` / `gt` / 字面量枚举 | 校验失败，异常里只带字段名与原因，不回显输入值 | `app/config/settings.py:871`（`hide_input_in_errors=True`） |
| `DATABASE_URL` 不以 `postgresql+asyncpg://` 开头 | 校验失败，并提示改用 Postgres | `app/config/settings.py:738` |
| `S3_BUCKET` 不匹配 S3 桶名正则 | 校验失败 | `app/config/settings.py:752` |
| `APP_ENV=production` 且 `AUTH_SECRET` 是默认占位值、含占位词、或短于 32 字符 | 启动失败，报错里给出生成命令 | `app/config/settings.py:779` |
| `APP_ENV=production` 且 `AUTH_COOKIE_SECURE=false` | 启动失败 | `app/config/settings.py:791` |
| `AUTH_COOKIE_NAME` 含非法字符，或用了 `__Host-` / `__Secure-` 前缀却没开 secure | 启动失败 | `app/config/settings.py:798` |
| 生效的 SameSite 为 `none` 但 `AUTH_COOKIE_SECURE=false` | 启动失败。注意「生效」：`CORS_ALLOW_CREDENTIALS=true` 会隐式把 SameSite 推到 `none` | `app/config/settings.py:830` |
| `CORS_ALLOW_CREDENTIALS=true` 且来源列表含 `*` | `create_app()` 抛 `ValueError`，uvicorn 导入失败退出 | `app/server.py:349` |
| `MCP_SERVERS` 声明格式错、名字非法或重复 | 启动失败（`load_server_configs` 的 `ValueError` 无兜底） | `app/tools/mcp_client.py:140` → `app/server.py:177` |
| 数据库连不上，或模型里有列而库里没有 | 启动中止：`verify_schema_at_head()` 抛错并给出可复制的 `ALTER TABLE` | `app/db/schema.py:20`、`app/server.py:101` |
| 提示词模板编译或渲染失败 | 启动中止 | `app/server.py:78` |

以下几项同样是配置问题，但**只记日志、不阻止启动**（都属于「值合法、环境不配合」）：

| 条件 | 行为 | 依据 |
|---|---|---|
| `READINESS_REQUIRED_DEPENDENCIES` 含未知名字 | 忽略该项并告警 | `app/api/ops.py:52` |
| `TRUSTED_PROXY_HEADERS` 有值但 `TRUSTED_PROXY_IPS` 为空 | 告警，标头一概不采信 | `app/server.py:60` |
| 反向代理部署但两项都没配 | 告警「认证限流按 client.host 计数」 | `app/server.py:49` |
| 对象存储不可达或未配置 | 告警，沙箱产物不会导出 | `app/server.py:154` |
| MCP server 连不上 | 告警，内置工具照常可用 | `app/server.py:197` |
| 提供商 API Key 解密失败（换了 `AUTH_SECRET`） | 逐条点名告警 | `app/server.py:134` |

## 最小可运行配置

严格来说没有必填项：89 个字段全有默认值，其中 `DATABASE_URL` 与 `REDIS_URL` 的默认值正好对上 `docker-compose.yml` 起的 postgres（`bennett:bennett@127.0.0.1:5432`）与 redis（`6379`）。真正的门槛是**依赖服务可连**——Postgres 必须有（启动即建表并校验漂移），Redis 可缺（降级），对象存储只在 `SANDBOX_ENABLED=true` 时需要。`LITELLM_API_BASE` 默认指向本机 4000 端口的 LiteLLM 代理，那个代理不在 compose 里，需要自备；网关不可达不影响启动（引导只写库、不联网），但对话时会失败。

所以最小 `.env` 要写的是两项依赖地址、一组网关凭据和一个签名密钥：

```bash
APP_ENV=development

# 必须在启动前存在：库与账号，compose 里有对应的 postgres 服务
DATABASE_URL=postgresql+asyncpg://bennett:bennett@127.0.0.1:5432/bennett

# 可缺：缺失时限流退化为进程内计数、缓存全部 miss
REDIS_URL=redis://127.0.0.1:6379/0

# 首次启动用它引导出一条「默认网关」提供商，之后以数据库为准
LITELLM_API_BASE=http://localhost:4000
LITELLM_API_KEY=<网关密钥>
MODEL_NAME=openai/gpt-4o

# 生成：python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
AUTH_SECRET=<上面命令的输出>
```

上线到 `APP_ENV=production` 时，上面这份还要改一行、补三项，否则起不来或语义不对：

```bash
APP_ENV=production
AUTH_SECRET=<≥32 字符的强随机串>
AUTH_COOKIE_SECURE=true          # 必须，前置反代要提供 HTTPS
CORS_ALLOWED_ORIGINS=https://app.example.com   # production 留空不回退本地端口
```

`AUTH_SECRET` 的取值不要自己编占位词：`change-me`、`replace-with`、`your-`、`example`、`placeholder`、`insecure` 这六个词按大小写不敏感匹配，命中任何一个都会被生产守卫拒绝，`your-secret-here` 这种「看起来很随机」的值同样不通过。`.env.example` 里的密钥值也都是占位，不能直接上生产。

## 不在 Settings 里的环境变量

以下变量由别的模块直接读环境，不受 pydantic 校验，也不会因为写错而启动失败。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `LOG_LEVEL` | `INFO` | 日志级别，只接受 `CRITICAL` / `ERROR` / `WARNING` / `INFO` / `DEBUG`（大小写不敏感），认不出退回 `INFO`（`app/config/logging_setup.py:54`）。`DEBUG` 下 httpx / openai 等第三方库仍被压在 `WARNING`。 |
| `LOG_ACCESS` | `False` | 是否输出 uvicorn 访问日志。`true/1/yes/on` 为真，`false/0/no/off` 为假，其余退回默认（`app/config/logging_setup.py:60`）。 |
| `OPENSANDBOX_INSECURE_SERVER` | 未设置 | 由 OpenSandbox server 自己读取，不属于本服务配置。设为 `YES` 才允许无鉴权启动，见 `scripts/demo/README.md`。 |

质量平台的变量由 `python -m quality` 这个独立 CLI 读取，不进服务进程，模块级读取意味着要在启动它之前设好；命令行参数可覆盖（`QUALITY_TIMEOUT_S` 是例外：拿到非数字会在导入时抛 `ValueError`）。

| 变量 | 默认值 | 说明 |
|---|---|---|
| `QUALITY_BASE_URL` | `http://127.0.0.1:3425/v1` | 被测模型的 OpenAI 兼容端点（`quality/targets/drivers.py:22`）。 |
| `QUALITY_API_KEY` | `magpie` | 被测端点密钥（`quality/targets/drivers.py:23`）。敏感。 |
| `QUALITY_MODEL` | `group/deepseek-v4.1-flash` | 被测模型标识（`quality/targets/drivers.py:24`）。 |
| `QUALITY_TIMEOUT_S` | `300` | 单次调用超时（秒）（`quality/targets/drivers.py:25`）。 |
| `QUALITY_LIVE_BASE_URL` | `http://127.0.0.1:8000` | 真实实例目标的本服务地址（`quality/targets/live.py:23`）。 |
| `QUALITY_LIVE_COOKIE` | `""` | 真实实例目标的会话 cookie，形如 `name=value`，多个用 `;` 分隔（`quality/targets/live.py:39`）。敏感。 |
| `QUALITY_ACTOR` | `local` | 写进审计行的执行人（`quality/cli.py:301`）。 |
| `QUALITY_TENANT` | `default` | 写进审计行的租户（`quality/cli.py:302`）。 |

平台用法见 [质量平台](../design/quality-platform.md)。

## 相关源码

- `app/config/settings.py`：字段定义、validator、`get_cors_origins()`
- `app/config/logging_setup.py`：`LOG_LEVEL` / `LOG_ACCESS`
- `app/server.py`：`create_app()` 的 CORS 校验与 lifespan 启动自检
- `app/db/schema.py`：建表与漂移检测
- `app/api/ops.py`：就绪探针与指标端点
- `app/api/rate_limit.py`：限流器、可信代理解析
- `app/tools/mcp_client.py`：`MCP_SERVERS` 解析与校验
- `.env.example`：带逐项注释的完整清单
