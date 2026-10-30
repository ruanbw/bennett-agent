---
title: API 与 SSE 契约
description: 端点分组与认证、限流桶、请求字段上限、SSE 分帧与事件类型、错误映射与兼容策略。
---

# API 与 SSE 契约

后端只暴露 `/api/*`，另有两个不带前缀的运维端点（`/health`、`/health/ready`），以及一个默认关闭、免登录的 `/api/metrics`。除注册、登录与 `/api/metrics` 外，所有端点都要求
HttpOnly Cookie 会话。这页给的是接口面的契约与约定：路径、方法、认证要求、限流桶、跨端形状。
逐字段的完整定义以 `/openapi.json` 为准（非 production 环境可从 `/docs` 查看）。

交互式文档在非 production 环境可用：`/docs`、`/redoc`、`/openapi.json`。

## 端点总表

### auth（`app/api/auth.py`）

| 方法 | 路径 | 认证 | 限流 | 说明 |
|---|---|---|---|---|
| POST | `/api/auth/register` | 无 | IP 桶 | 邮箱 + 密码（≥8 位）。恒返回 202，不泄露邮箱是否存在 |
| POST | `/api/auth/login` | 无 | IP 桶 | **表单编码**（`username` / `password`），成功 204 + Set-Cookie |
| POST | `/api/auth/logout` | Cookie | 无 | 删服务端会话行并清 cookie。需有效会话，重复调用返回 401，客户端按 401 视为已登出 |
| GET | `/api/auth/me` | Cookie | 无 | 当前用户（含 `role`），前端用它判登录态 |
| POST | `/api/auth/change-password` | Cookie | 账号桶 | 校验当前密码；成功后该账号全部会话立即失效 |

注册时新账号角色固定为 `user`：没有任何请求参数能指定角色。
管理员身份只能由 `scripts/seed_admin.py` 播种或由超管在后台授予。

### chat（`app/api/chat.py`）

| 方法 | 路径 | 认证 | 限流 | 说明 |
|---|---|---|---|---|
| GET | `/api/models` | Cookie | 无 | 可用模型列表（含窗口、思考等级、默认项） |
| POST | `/api/chat/title` | Cookie | 账号桶 | 由首条提示词生成会话标题，失败回退截断首行 |
| POST | `/api/chat` | Cookie | 账号桶 | SSE 流式对话 |

### capabilities（`app/api/capabilities.py`）

| 方法 | 路径 | 认证 | 限流 | 说明 |
|---|---|---|---|---|
| GET | `/api/capabilities` | Cookie | 无 | 运行时能力快照：实际装配的工具、是否可联网、是否可跑代码、MCP server 状态 |

要求登录：能力清单暴露服务端装了哪些工具，属于部署信息。

### sandbox（`app/api/sandbox.py`）

| 方法 | 路径 | 认证 | 限流 | 说明 |
|---|---|---|---|---|
| POST | `/api/sandbox/heartbeat` | Cookie | 无 | 刷新「客户端还在」标记，恒 204 |
| GET | `/api/sandbox/status` | Cookie | 无 | 该用户最近一条租约的状态；没有过机器时 404 |
| GET | `/api/sandbox/artifacts` | Cookie | 无 | 产物清单 + 预签名下载直链（没有产物时 200 + 空列表；未启用执行环境时 404） |

**没有直接 exec 的接口**。执行类操作只经由 Agent 的工具调用，否则会多一个必须与工具层
同等严格、却只服务「自己敲命令」的授权面。三个端点都要求 `session_id`（心跳在 body，
两个 GET 在查询串），但它只校验形状、不参与寻址：机器按用户分，查询一律带 `user_id`。

### sync（`app/api/sync.py`）

| 方法 | 路径 | 认证 | 限流 | 说明 |
|---|---|---|---|---|
| POST | `/api/sync/push` | Cookie | 账号桶 | 批量推送会话与消息，逐行接受或拒绝，幂等 |
| GET | `/api/sync/pull` | Cookie | 账号桶 | 按不透明游标增量拉取，无变化时长轮询 |

### admin（`app/api/admin.py`）

整个 router 挂在 `current_admin`（未登录 401，role < admin 403）。

| 方法 | 路径 | 额外权限 | 说明 |
|---|---|---|---|
| GET | `/api/admin/providers` | admin | 提供商列表（API Key 只回掩码） |
| GET | `/api/admin/provider-presets` | admin | 内置厂商预设目录（静态） |
| POST | `/api/admin/providers` | admin | 新增提供商，地址当场做 SSRF 校验 |
| PATCH | `/api/admin/providers/{id}` | admin | 更新；**换地址必须重填 API Key** |
| DELETE | `/api/admin/providers/{id}` | admin | 删除（连带其模型） |
| POST | `/api/admin/providers/{id}/test` | admin | 连通性测试；失败返回 200 + `ok=false` |
| POST | `/api/admin/providers/{id}/sync` | admin | 同步模型列表；上游故障 502 |
| GET | `/api/admin/providers/{id}/models` | admin | 某提供商下的模型 |
| POST | `/api/admin/providers/{id}/models` | admin | 手动添加模型；重复 409 |
| PATCH | `/api/admin/models/{uuid}` | admin | 更新模型（展示名、启用、默认） |
| DELETE | `/api/admin/models/{uuid}` | admin | 删除模型 |
| GET | `/api/admin/users` | admin | 搜索 / 按状态筛选 / 分页列出用户 |
| POST | `/api/admin/users/{id}/ban` | admin | 封禁并踢下线；不能封自己或同级 |
| POST | `/api/admin/users/{id}/unban` | admin | 解封；等级校验与封禁完全对称 |
| PATCH | `/api/admin/users/{id}/role` | **superadmin** | 改角色；不能改自己、不能改已封禁账号、不能降级最后一个超管 |

封禁会连带删除该用户的全部会话行，并回收他名下的沙箱机器——机器必须和会话一起失效，
否则被改过的客户端仍能用已有租约烧算力。

### ops（`app/api/ops.py`）

| 方法 | 路径 | 认证 | 说明 |
|---|---|---|---|
| GET | `/health` | 无 | 存活探针，不碰数据库与上游 |
| GET | `/health/ready` | 无 | 就绪探针：database / redis / storage / provider_keys 逐项 |
| GET | `/api/metrics` | 无（默认关闭） | Prometheus 文本格式；`METRICS_ENABLED=false` 时返回 404 |

`/health` 与 `/health/ready` 不出现在 OpenAPI 里（`include_in_schema=False`），
`/api/metrics` 同理。`/health/ready` 只把 `READINESS_REQUIRED_DEPENDENCIES`
（默认 `database`）的失败判 503，其余降级为 `degraded`（仍 200）。

## 认证与限流

### 认证

会话是服务端状态（`accesstoken` 表），不是 JWT：登出、改密、封禁、改角色都立即生效。
库里的 `token` 列存 SHA-256 摘要，库被读也无法直接回放。cookie 名取 `AUTH_COOKIE_NAME`
（默认 `bennett_session`）：用 curl 调试时靠 `-c/-b` 保存与回传，它不归前端脚本管。

依赖三个：`current_active_user`（401）、`current_admin`（403）、
`current_superadmin`（403）。401 表示没有登录态，403 表示权限不够——
前端靠这个区分「重新登录」与「找管理员」。

### 限流

| 桶 | 键 | 默认配额 | 端点 |
|---|---|---|---|
| 认证 | 来源 IP + 路径末段（login 与 register 分桶） | 10 / 分钟 | `/api/auth/login`、`/register` |
| 对话 | `user:<id>` | 30 / 分钟 | `/api/chat`、`/api/chat/title` |
| 改密 | `user:<id>` | 5 / 分钟 | `/api/auth/change-password` |
| 同步 | `user:<id>` | 120 / 分钟 | `/api/sync/*` |

配额为 0 表示不限流，但依赖仍然生效：声明了 `current_active_user` 的限流函数在配额为 0 时
提前 return，登录要求依然成立。

计数存储默认 Redis（跨实例共享配额），Lua 脚本在同一脚本里完成「裁剪 + 计数 + 过期」。
Redis 不可用时按 `RATE_LIMIT_ON_REDIS_ERROR` 处置：`local_fallback`（默认，退到进程内计数）、
`fail_closed`（一律 429）、`fail_open`（一律放行）。三种都会告警并计入
`redis_unavailable_total`，静默降级不允许。

代理标头只有同时配了 `TRUSTED_PROXY_HEADERS` 与 `TRUSTED_PROXY_IPS` 才采信，
且直连对端必须落在受信网段内：能被直连的后端任人自填 `X-Forwarded-For` 就能换桶。

### 限流响应头

429 响应带：

```text
Retry-After: 12
X-RateLimit-Limit: 30
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 12
```

`X-RateLimit-*` 由 `expose_headers` 显式放行——不写这一行浏览器只放行 6 个简单响应头，
前端读不到限流信息，没法告诉用户还要等几秒。

## 请求响应 schema 要点

### `ChatRequest`（`app/schemas/chat.py`）

| 字段 | 约束 | 说明 |
|---|---|---|
| `model` | ≤300 字符，空白视作未指定 | 必须在白名单里，否则 400 |
| `reasoning_effort` | ≤32 字符，字符集 `[A-Za-z0-9._-]` | 必须在该模型声明的等级内，否则 400 |
| `messages` | 1..100 条 | 首条必须 user、严格交替、末条 user（续跑时可为带 `tool_calls` 的 assistant） |
| `context_anchor` | 可选 | 上一轮真实用量锚点；下标须指向 assistant |
| `session_id` | `sess_` 前缀，≤64 字符 | 会话标识，不是身份 |
| `approvals` | ≤50 条 | `tool_call_id → bool`；id 必须出现在历史里 |

消息内容上限：`content` 32000 字符、`thinking` 64000 字符、历史总量 400000 字符。
`ChatMessage.role` 只允许 `user` / `assistant`——`system` 会被插进历史，
等于让调用方注入系统提示词。`tool_calls` 只允许挂在 assistant 消息上，且不收结果文本：
工具结果由服务端产生，回放等于允许伪造「删除已成功」。

`content` 可以是字符串或分片数组；非文本分片（`image` / `file`）直接拒绝并说明
「后端目前只接受纯文本」。静默丢掉更糟：用户以为图片传上去了，模型只看到一句文字。

### 同步的请求响应

- `PushRequest` 的行带 `client_rev`（客户端修订号）与 `created_at`（毫秒时间戳）。
  服务端按 `client_rev` 拒绝更旧的重放；`created_at` 只用于展示排序，不作同步判据。
- `PushResult` 带本次 push 之后的 `version`，并逐行给结果：`accepted`（会话 / 消息计数）与
  `rejected`（每行一个 `reason`）。
  原因枚举：`not_owned` / `stale` / `too_large` / `conversation_missing` /
  `conversation_deleted` / `quota_exceeded`。
- `PullResult` 带 `cursor`、`has_more` 与两路数据。`has_more` 为真时游标停在
  「两路都拉完」的位置，同一行可能重复返回，客户端必须幂等。
- `GET /api/sync/pull` 的三个查询参数：`cursor`（首次同步不传；不认识回 400，指向未来回 410）、`limit`
  （每个数据流的行数上限，默认 `SYNC_PULL_MAX_ROWS`）、`timeout`（长轮询挂起上限，0 表示立即返回，
  服务端再用 `SYNC_LONG_POLL_SECONDS` 夹一次）。首次同步与 `has_more` 为真的分页都不挂起。

## SSE 契约

### 分帧

每帧一个 JSON 对象，`type` 作判别键：

```text
data: {"type":"content","delta":"你"}
```

`data: ` 前缀与结尾空行由 `sse-starlette` 的 `ServerSentEvent` 加，
`app/api/chat.py` 只负责把事件压成一行 JSON（`model_dump_json()`，
裸换行会被转义成 `\n`，分帧才安全）。客户端按 `\r?\n\r?\n` 分帧。

### 事件类型

| `type` | 关键字段 | 何时发 |
|---|---|---|
| `content` | `delta` | 正文增量 |
| `thinking` | `delta` | 思考过程增量 |
| `tool_call` | `tool_call_id`、`tool_name`、`args`（字符串） | 工具开始调用 |
| `tool_result` | `result`、`outcome` | 工具返回；`outcome` ∈ success/failed/denied/interrupted |
| `clarification` | `question`、`options`、`allow_custom_input` | 模型发起澄清提问 |
| `approval_required` | `tool_calls[]`（含 `description`、`risk_level`） | 有调用待批准，本轮到此结束 |
| `fallback` | `from_model`、`to_model`、`reason` | 已切到备用模型 |
| `compaction` | `strategy`、`dropped_messages`、`used_tokens_before/after`、`limit_tokens` | 本轮开始前做过压缩 |
| `usage` | `context_tokens`、`context_window`、`context_limit_tokens`、`output_tokens`、`cache_read_tokens`、`timing` | 流结束前；上游没报就不发 |
| `done` | — | 正常结束 |
| `error` | `message`、`retryable`、`code` | 本轮失败 |

事件流必以 `done` 或 `error` 收尾。`usage` 里量不到的部分整块缺席，绝不补 0。

`compaction` 只给数字与策略，不下发摘要正文：摘要可能含早期对话的敏感内容，
而客户端会落库并同步到其他设备。

### 响应头

```text
Content-Type: text/event-stream
Cache-Control: no-store
X-Accel-Buffering: no
```

`X-Accel-Buffering: no` 关掉中间层缓冲，否则流式输出会被反向代理攒成一整块。

`Connection: keep-alive` 被显式移除（`ChatEventSourceResponse`）：sse-starlette 3.5.0
在合并 headers 之后仍无条件写入该标头，而 HTTP/2 禁止它，网关会重置流。
只能从 `raw_headers` 里删，改 `SSE_HEADERS` 拦不住。

另外每次 ASGI send 有 30s 超时：半开连接下 `await send` 会无限阻塞，一直占着上游额度。

### 审批续跑

审批在 SSE 上分两段。首轮以 `approval_required` 结束，客户端把 `tool_calls` 原样持久化
（含 `tool_call_id` 与 `args`）。用户决定后，客户端把历史与
`approvals: {tool_call_id: bool}` 一起回传。

服务端只看历史形状，不看客户端声明：历史里「有调用、无结果」的 id 才需要决定，
**缺失的一律按拒绝处理**。

## 错误映射

### HTTP 层

| 状态码 | 何时 | `detail` 形状 |
|---|---|---|
| 400 | 请求不合法（模型不在白名单、思考等级不支持、游标不认识、改密凭据错） | 中文句子或 `CODE_原因` |
| 401 | 没有有效登录态 | `Unauthorized` |
| 403 | 权限不够、跨站请求被拒、注册已关闭 | 中文句子 |
| 404 | 资源不存在（沙箱从未有过机器、未启用执行环境） | 中文句子；`/api/metrics` 关闭时是空 body |
| 409 | 冲突（模型重复、模型所属提供商不存在） | 中文句子 |
| 410 | 游标失效，需全量重同步 | 含 `code=resync_required` |
| 413 | 请求体超限 | `请求体过大，已拒绝` |
| 429 | 限流 | `请求过于频繁，请在 N 秒后重试` + 限流头 |
| 500 | 未捕获异常 | `服务器内部错误，请重试（错误编号 <id>）` |
| 502 | 上游网关故障（同步模型列表） | 上游错误摘要 |
| 503 | 无可用模型、就绪探针必需依赖失败 | 中文句子 |

500 响应体里的 `error_id` 与响应头 `X-Request-ID` 同值，用户从 devtools 就能拿到，
不必等错误文案。异常原文可能含 SQL、路径与凭据，只进日志。

422 是 FastAPI 的请求体校验失败，body 里是 pydantic 的逐字段错误。

400 里有一类不是「字段不合法」而是整批拒绝：单次 push 超过 `SYNC_PUSH_MAX_ROWS`（默认 200）行时整批回
`too_many_rows`。分批是客户端的事，逐行拒绝会让一半行进库、一半行回同一个原因。

### SSE 层

流式对话自己下发 `error` 事件（SSE 已发 200），不经 HTTP 错误映射：

```json
{"type":"error","message":"...","retryable":false,"code":"context_overflow"}
```

`code` 取值：`context_overflow`（重试必败）、`model_unavailable`（配置里没有该模型）、
`internal_error`（服务端自身错）。`retryable` 只决定前端是否显示重试按钮：
400/401/403/404 这类重试结果一样，标 `false`。

## OpenAPI 与生产默认关闭

`_api_docs_enabled()` 的规则：`ENABLE_API_DOCS` 为 `None` 时按环境自动——
production 关闭，其余环境开启；显式设 true/false 可覆盖。

生产默认关闭的理由：这些页面把端点结构白送给未认证访客。OpenAPI 里会额外声明一个
cookie 类型的 `APIKeyCookie` 安全方案，并给除公开端点外的所有操作加上 `security`，
让 `/docs` 标出哪些端点需要认证。

公开端点集合是显式常量（`/api/auth/register`、`/api/auth/login`）：新增公开端点必须改这里，
`tests/test_server.py` 会逼你做这个决定。

## 兼容策略

| 变更 | 兼容做法 |
|---|---|
| SSE 事件新增字段 | 客户端按 `type` 分派，新字段忽略即可；服务端新增字段一律带默认值（`fallback.reason` 就是这样，老服务端不带时前端按「未说明的原因」处理） |
| SSE 新增事件类型 | 老客户端落到默认分支忽略；不要复用已有 `type` 改语义 |
| 事件字段删除 | 先废弃一个版本再删 |
| 同步游标格式 | 换前缀即可：旧游标被识别为不认识 → 400 `invalid_cursor`；指向未来的游标 → 410 `resync_required` |
| 同步行结构 | `extra="forbid"`：客户端多传字段会 422，逼着双方同时升级 |
| 提示词行为变更 | 用 `quality/` 的评测对比基线，而不是靠「感觉变差了」 |

同步协议里有两处刻意的不对称：删会话不拆机器（机器按用户分，同一人的其它会话可能正靠它跑着）；
删掉会话后写入的消息按 `conversation_deleted` 拒绝而不是复活会话。

## 相关源码

- `app/api/chat.py` — SSE 帧、限流依赖、模型解析
- `app/api/sync.py` — push/pull 与游标错误映射
- `app/api/rate_limit.py` — 限流器、客户端 IP 解析、429 头
- `app/schemas/events.py` — 事件契约（`type` 判别联合）
- `app/schemas/chat.py` — 请求校验与上限
- `app/schemas/sync.py` — 同步请求响应与拒绝原因
- `app/server.py` — OpenAPI 装配、安全方案、公开端点集合
- `app/observability.py` — 请求 ID 与 HTTP 指标
