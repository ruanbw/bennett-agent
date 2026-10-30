---
title: HTTP 端点目录
description: 32 个 HTTP 端点的路径、鉴权、限流与请求响应模型字段，附最小 curl 走查。
---

# HTTP 端点目录

这页是查阅用的清单：每个端点的路径、鉴权要求、限流桶、请求与响应模型，以及每个字段的类型与含义。
契约与取舍写在 [API 与 SSE 契约](../design/api.md)。

## 怎么用这份目录

- 按模块分节，每节一张表。`鉴权` 列写的是依赖注入里的当前用户判定：`无` = 公开端点，`Cookie` = `current_active_user`，
  `admin` = `current_admin`，`superadmin` = `current_superadmin`。
- `请求 → 响应` 列给模型名，字段说明在同名的模型小节里；`—` 表示该端点的请求或响应没有结构化 body。
- `限流` 列的配额是 `app/config/settings.py` 的默认值，部署时用对应环境变量改，`0` 表示不限流。
- 全部路径以 `app/api/*.py` 里各 router 的 `prefix` 为准拼接：`/api/auth`、`/api`（chat 与 capabilities）、
  `/api/sandbox`、`/api/sync`、`/api/admin`，以及不带前缀的运维端点。
- 非 production 环境可以打开 `/docs`、`/redoc`、`/openapi.json` 直接试（`ENABLE_API_DOCS` 控制，production 默认关闭）。

## 通用约定

### 认证

会话是服务端状态，不是 JWT：登录成功后由服务端写一个 HttpOnly cookie（`AUTH_COOKIE_NAME`，默认 `bennett_session`），
值是 `access_token` 表里某一行的凭据，登出、改密、封禁、改角色都能立刻让它失效。

401 表示没有有效登录态（cookie 缺失、过期或用户已停用），403 表示登录了但权限不够。前端靠这个区分
「重新登录」与「找管理员」。用 curl 调试时要 `-c` 存 cookie、`-b` 回传：

```bash
curl -c cookie.txt ...   # 登录，把 Set-Cookie 存下来
curl -b cookie.txt ...   # 后续请求带上它
```

### 错误响应

| 状态码 | 来源 | body |
|---|---|---|
| 400 / 403 / 404 / 409 / 410 / 429 / 502 / 503 | `HTTPException` | `{"detail": "..."}`，部分带机器可读的 `code=...` 后缀 |
| 413 | `MaxBodySizeMiddleware` | `{"detail": "请求体过大，已拒绝"}` |
| 403（跨站） | `OriginGuardMiddleware` | `{"detail": "跨站请求被拒绝"}` |
| 422 | FastAPI 请求体校验 | pydantic 的逐字段错误列表 |
| 500 | 未捕获异常处理器 | `{"detail": "服务器内部错误，请重试（错误编号 <id>）", "error_id": "<id>"}` |

500 的 `error_id` 与响应头 `X-Request-ID` 同值。异常原文（可能含 SQL、路径、凭据）只进日志。
SSE 对话是例外：HTTP 已经返回 200，失败在流里以 `{"type":"error"}` 事件下发，见 [API 与 SSE 契约](../design/api.md)。

请求体硬上限是 `MAX_REQUEST_BODY_BYTES`（默认 32 MiB），超限在读取阶段就返回 413，body 不会被读进内存。

### 限流

| 桶 | 键 | 默认配额 | 端点 |
|---|---|---|---|
| 认证 | 来源 IP + 路径末段（login 与 register 各自分桶） | 10 / 分钟 | `POST /api/auth/login`、`POST /api/auth/register` |
| 对话 | `user:<id>` | 30 / 分钟 | `POST /api/chat`、`POST /api/chat/title` |
| 改密 | `user:<id>` | 5 / 分钟 | `POST /api/auth/change-password` |
| 同步 | `user:<id>` | 120 / 分钟 | `POST /api/sync/push`、`GET /api/sync/pull` |

被限流时返回 429，带四个响应头：

```text
Retry-After: 12
X-RateLimit-Limit: 30
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 12
```

`X-RateLimit-*` 在 CORS 的 `expose_headers` 里显式放行，浏览器端才读得到。
计数默认存 Redis（跨实例共享配额），Redis 不可用时按 `RATE_LIMIT_ON_REDIS_ERROR` 处置。

### 分页与游标

- `GET /api/admin/users` 用 `limit` / `offset`，响应里的 `total` 是**筛选后**的总数。
- `GET /api/sync/pull` 用不透明游标：响应给 `cursor`，客户端原样回传；`has_more` 为真时同一行可能重复返回，
  客户端必须幂等。游标格式变化时旧游标会被识别成不认识（400 `invalid_cursor`），指向未来的游标是 410 `resync_required`。

## auth（`app/api/auth.py`，prefix `/api/auth`）

| 方法 | 路径 | 鉴权 | 限流 | 请求 → 响应 | 说明 |
|---|---|---|---|---|---|
| POST | `/api/auth/login` | 无 | IP 桶 | 表单 → 204 | 必须发 `application/x-www-form-urlencoded`，字段 `username`（填邮箱）与 `password`；发 JSON 直接 422，凭据错误 400 |
| POST | `/api/auth/logout` | Cookie | 无 | — → 204 | 删掉 `access_token` 表里那一行，该会话立刻失效；不带 cookie 时 401 |
| POST | `/api/auth/register` | 无 | IP 桶 | `UserCreate` → 202 空 body | 邮箱已存在也返回 202（不做邮箱枚举）；密码不达标 400；`REGISTRATION_ENABLED=false` 时 403 |
| POST | `/api/auth/change-password` | Cookie | 账号桶 | `PasswordChangeRequest` → 204 | 必须验证当前密码；成功后该账号全部会话（含本次）立即失效 |
| GET | `/api/auth/me` | Cookie | 无 | — → `UserRead` | 前端启动时用它判断登录态；`role` 每次请求都从库里读 |

登录与登出的成功状态码都是 204（cookie transport 的约定），响应里没有 body，会话在 `Set-Cookie` 里。

### `UserCreate`

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `email` | EmailStr | 是 | 登录名，同时是账号唯一键 |
| `password` | str | 是 | 至少 8 个字符（`MIN_PASSWORD_LENGTH`），只设下限 |
| `is_active` | bool \| null | 否 | 默认 `true`。落库前被 `safe=True` 的 `create_update_dict()` 剔除，传了也不生效 |
| `is_superuser` | bool \| null | 否 | 默认 `false`，同上被剔除：注册接口无法自封管理员 |
| `is_verified` | bool \| null | 否 | 默认 `false`，同上被剔除 |

### `UserRead`

| 字段 | 类型 | 含义 |
|---|---|---|
| `id` | UUID | 用户主键 |
| `email` | str | 邮箱 |
| `is_active` | bool | `false` 即已封禁 |
| `is_superuser` | bool | 由 `role` 派生，`role >= admin` 时为真 |
| `is_verified` | bool | 没有邮箱验证流程；管理员建号时置真 |
| `role` | str | `user` / `admin` / `superadmin`，鉴权以它为准而不是 `is_superuser` |

### `PasswordChangeRequest`

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `current_password` | str（1..128） | 是 | 当前密码；错则 400 `CHANGE_PASSWORD_BAD_CREDENTIALS` |
| `new_password` | str（1..128） | 是 | 与当前密码相同则 400 `CHANGE_PASSWORD_SAME`；长度与强度走与注册同一条校验 |

## chat（`app/api/chat.py`，prefix `/api`）

| 方法 | 路径 | 鉴权 | 限流 | 请求 → 响应 | 说明 |
|---|---|---|---|---|---|
| GET | `/api/models` | Cookie | 无 | — → `ModelListResponse` | 库已接管时只列启用中的模型（读库、不走缓存），否则回落到网关的模型列表 |
| POST | `/api/chat/title` | Cookie | 账号桶 | `TitleGenerateRequest` → `TitleGenerateResponse` | 由首条提示词生成极简会话标题 |
| POST | `/api/chat` | Cookie | 账号桶 | `ChatRequest` → SSE 流 | `text/event-stream`；模型不在白名单 400，一个模型都没启用 503 |

`POST /api/chat` 的响应头固定带 `Content-Type: text/event-stream`、`Cache-Control: no-store`、
`X-Accel-Buffering: no`，并显式去掉 `Connection: keep-alive`（HTTP/2 禁止该标头，网关会重置流）。
每帧是一个 JSON 对象，`type` 作判别键，事件清单见 [API 与 SSE 契约](../design/api.md)。

### `ChatRequest`

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `model` | str \| null（≤300） | 否 | 路由键 `{provider.slug}/{model_id}`；不传取后台默认，传了必须在可用模型白名单里 |
| `reasoning_effort` | str \| null（≤32，`[A-Za-z0-9._-]`） | 否 | 思考等级；必须在该模型声明的等级内，`none` 始终放行 |
| `messages` | `ChatMessage[]`（1..100） | 是 | 历史与当前对话，首条必须 user、严格交替、末条 user（续跑审批时可为带 `tool_calls` 的 assistant） |
| `context_anchor` | `ContextAnchor` \| null | 否 | 上一轮的真实用量锚点；缺失时服务端自己估算，不报错 |
| `session_id` | str \| null（`sess_...`，≤64） | 否 | 客户端生成的会话标识，供沙箱按会话分配机器；不是身份 |
| `approvals` | `{tool_call_id: bool}`（≤50 条） | 否 | 待审批工具调用的决定；id 必须在历史里出现过，缺失的一律按拒绝处理 |

历史总长度上限 `MAX_HISTORY_CHARS` = 400000 字符（`content` 与 `thinking` 之和）。

### `ChatMessage`

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `role` | `"user"` \| `"assistant"` | 是 | 不收 `system`：那等于让调用方注入系统提示词 |
| `content` | str（≤32000） | 是 | 正文；user 消息不能是空白，assistant 可为空串（只有思考过程的那一轮） |
| `thinking` | str \| null（≤64000） | 否 | 思考过程原文 |
| `tool_calls` | `ReplayedToolCall[]`（≤50） | 否 | 只允许挂在 assistant 上；user 带了等于伪造模型行为 |

`content` 也接受前端库的分片数组：纯文本分片会被拼成一个字符串，`image` / `file` 分片直接 422。

### `ReplayedToolCall`

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `tool_call_id` | str（1..200） | 是 | 上游生成的调用 id |
| `tool_name` | str（1..128） | 是 | 工具名 |
| `args` | str（≤64000，默认 `"{}"`） | 否 | 调用入参，JSON 文本；保持字符串，解析失败时原样拒绝 |

不收 `outcome` 或工具结果文本：工具结果由服务端产生，回放等于允许伪造「删除已成功」。

### `ContextAnchor`

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `message_index` | int（≥0） | 是 | 锚点在 `messages` 里的下标，必须指向 assistant 消息 |
| `context_tokens` | int（>0） | 是 | 那一轮上游回报的 prompt token 数 |

### `ModelListResponse`

| 字段 | 类型 | 含义 |
|---|---|---|
| `models` | `ModelItem[]` | 启用中的模型 |
| `default_model` | str | 默认模型的路由键：后台显式设过就用它，否则取第一条启用模型；一个都没启用时是空串 |

`ModelItem` 的字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `id` | str | 路由键 `{slug}/{model_id}`，对话时填进 `ChatRequest.model` |
| `name` | str | 展示名 |
| `provider` | str | 提供商显示名 |
| `model_id` | str \| null | 上游真实模型 ID；网关路径可能为空 |
| `description` | str \| null | 模型说明；库里没填时由「提供商名 + 展示名」拼出 |
| `reasoning` | bool \| null | 三态：`null` 表示网关没给这个字段 |
| `supported_reasoning_levels` | str[] | 该模型接受的思考等级 |
| `context_window` | int \| null | 上下文窗口（token） |
| `max_output_tokens` | int \| null | 输出上限（token） |
| `is_default` | bool | 是否为默认模型 |

### `TitleGenerateRequest` / `TitleGenerateResponse`

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `model` | str \| null（≤300） | 否 | 用哪个模型生成；不传取默认 |
| `prompt` | str（1..8000） | 是 | 首条用户提示词；纯空白会 422 |

响应只有一个字段 `title`（str）。

## capabilities（`app/api/capabilities.py`，prefix `/api`）

| 方法 | 路径 | 鉴权 | 限流 | 请求 → 响应 | 说明 |
|---|---|---|---|---|---|
| GET | `/api/capabilities` | Cookie | 无 | — → `CapabilitiesResponse` | 读 `build_tools()` 的实际装配结果与已缓存的 MCP 发现结果，端点本身不联网 |

要求登录：能力清单暴露服务端装了哪些工具，属于部署信息。

### `CapabilitiesResponse`

| 字段 | 类型 | 含义 |
|---|---|---|
| `web_search` | bool | 联网搜索工具（`tavily_search`）是否已装配 |
| `sandbox` | bool | 四个 `sandbox_*` 工具是否已装配 |
| `tools` | str[] | 本轮装配给 Agent 的工具名，按注册顺序 |
| `mcp_servers` | `McpServerStatus[]` | 已配置且连通的 MCP server；连不上的不在这里 |
| `mcp_error` | str \| null | 配了但没连上的原因；全部连通或未配置时为 `null` |

`McpServerStatus` 两个字段：`name`（server 名，也是工具名前缀）、`tools`（该 server 的工具名，不含前缀）。

## sandbox（`app/api/sandbox.py`，prefix `/api/sandbox`）

| 方法 | 路径 | 鉴权 | 限流 | 请求 → 响应 | 说明 |
|---|---|---|---|---|---|
| POST | `/api/sandbox/heartbeat` | Cookie | 无 | `SandboxHeartbeatRequest` → 204 | 会话页面可见时每 30 秒一次；恒 204，机器存在性无法探测 |
| GET | `/api/sandbox/status` | Cookie | 无 | query → `SandboxStatusResponse` | 该用户最近一条租约（含已销毁的）；从来没有过环境 404 |
| GET | `/api/sandbox/artifacts` | Cookie | 无 | query → `SandboxArtifactsResponse` | 把 `artifact_uri` 翻成预签名直链；恒 200（沙箱关闭时 404） |

没有直接 exec 的端点：执行只经由 Agent 的工具调用。两个 GET 都要求 `?session_id=sess_...`（≤64 字符，
模式与心跳一致），但它只校验形状、不参与查询：机器按用户分，谎报别人的 id 也只能查到自己的租约。
`SANDBOX_ENABLED=false` 时状态与产物端点返回 404 `未启用执行环境`。

### `SandboxHeartbeatRequest`

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `session_id` | str（`sess_...`，≤64） | 是 | 当前打开的会话标识 |

### `SandboxStatusResponse`

| 字段 | 类型 | 含义 |
|---|---|---|
| `state` | str | `provisioning` / `ready` / `paused` / `destroying` / `destroyed` / `failed` / `destroy_failed` |
| `expires_at` | datetime \| null | 硬 TTL 到点时间 |
| `artifact_uri` | str \| null | 回收后产物在对象存储里的键；没有产物或还没导出时为 `null` |

### `SandboxArtifactsResponse`

| 字段 | 类型 | 含义 |
|---|---|---|
| `session_id` | str | 请求里带的会话标识，仅回显 |
| `lease_id` | str \| null | 最近一条租约的 id；从未创建过为 `null` |
| `state` | str \| null | 租约状态；从未创建过为 `null` |
| `artifacts` | `SandboxArtifact[]` | 已导出的产物；没有产物时为空数组 |

`SandboxArtifact` 四个字段：`name`（文件名，目前是 `workspace.tar.gz` 与 `manifest.txt`）、
`size_bytes`（对象大小）、`download_url`（预签名直链，有时效，别缓存别转发）、`expires_at`（链接失效时间）。
对象存储未配置时降级成空数组，而不是 503。

## sync（`app/api/sync.py`，prefix `/api/sync`）

| 方法 | 路径 | 鉴权 | 限流 | 请求 → 响应 | 说明 |
|---|---|---|---|---|---|
| POST | `/api/sync/push` | Cookie | 同步桶 | `PushRequest` → `PushResult` | 逐行接受或拒绝，整批不因某行坏掉而失败；超行数上限 400 `too_many_rows` |
| GET | `/api/sync/pull` | Cookie | 同步桶 | query → `PullResult` | 不透明游标增量拉取；无变化时长轮询 |

### `PushRequest`

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `conversations` | `ConversationIn[]` | 否 | 会话行，默认空数组 |
| `messages` | `MessageIn[]` | 否 | 消息行，默认空数组 |

两个模型都是 `extra="forbid"`：多传字段会 422。单次合计行数上限 `SYNC_PUSH_MAX_ROWS`（默认 200）。

`ConversationIn` 的字段：

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `id` | str（`[A-Za-z0-9_-]{1,64}`） | 是 | 客户端生成的会话 id |
| `title` | str（≤2000，默认 `""`） | 否 | 标题 |
| `model` | str \| null（≤255） | 否 | 该会话使用的模型路由键 |
| `reasoning_effort` | str \| null（≤32） | 否 | 该会话的思考等级 |
| `is_pinned` | bool（默认 `false`） | 否 | 是否置顶 |
| `deleted` | bool（默认 `false`） | 否 | 墓碑：删除只推这一位，服务端据此级联删消息 |
| `client_rev` | int（0..2^31-1，默认 0） | 否 | 客户端修订号，每次修改 +1；服务端拒绝更旧的重放 |
| `created_at` | int（毫秒时间戳） | 是 | 只用于展示排序，不作同步判据 |
| `updated_at` | int（毫秒时间戳） | 是 | 同上 |

`MessageIn` 的字段：

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `id` | str（`[A-Za-z0-9_-]{1,64}`） | 是 | 客户端生成的消息 id |
| `conversation_id` | str（同上） | 是 | 所属会话 id |
| `role` | `"user"` \| `"assistant"` | 是 | 消息角色 |
| `payload` | object（默认 `{}`） | 否 | 整包透传：正文、思考链、工具调用与结果；服务端只校验大小与角色 |
| `client_rev` | int（0..2^31-1，默认 0） | 否 | 客户端修订号 |
| `created_at` | int（毫秒时间戳） | 是 | 客户端时间戳 |

### `PushResult`

| 字段 | 类型 | 含义 |
|---|---|---|
| `version` | int | 本次 push 之后该用户的同步版本；纯重放不变 |
| `accepted` | `PushAccepted` | 接受的行数，含 `conversations` 与 `messages` 两个计数 |
| `rejected` | `RejectedRow[]` | 逐行拒绝的原因 |

`RejectedRow` 三个字段：`kind`（`conversation` / `message`）、`id`、`reason`。`reason` 取值：

| 值 | 含义 |
|---|---|
| `not_owned` | 该 id 属于另一个用户 |
| `stale` | 服务端已有更新的 `client_rev`，直接丢弃 |
| `too_large` | payload 序列化后超过 `SYNC_PAYLOAD_MAX_BYTES`（默认 256 KiB） |
| `conversation_missing` | 会话既不在本批，也不在自己名下 |
| `conversation_deleted` | 会话已删除（墓碑存在），不能再写入 |
| `quota_exceeded` | 触到单用户会话数或单会话消息数上限 |

### `PullResult`

| 字段 | 类型 | 含义 |
|---|---|---|
| `cursor` | str | 下次请求原样回传 |
| `has_more` | bool（默认 `false`） | 还有下一页；此时同一行可能重复返回 |
| `conversations` | `ConversationOut[]` | 会话行 |
| `messages` | `MessageOut[]` | 消息行 |

`ConversationOut` 比入参多一个服务端字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `id` / `title` / `model` / `reasoning_effort` / `is_pinned` / `deleted` / `client_rev` / `created_at` / `updated_at` | 同 `ConversationIn` | 原样回显 |
| `version` | int | 服务端同步版本号，游标比较的对象 |

`MessageOut` 同理，另有两个服务端字段：

| 字段 | 类型 | 含义 |
|---|---|---|
| `id` / `conversation_id` / `role` / `payload` / `client_rev` / `created_at` | 同 `MessageIn` | 原样回显 |
| `seq` | int | 服务端赋的会话内顺序；客户端有它就按它排 |
| `version` | int | 服务端同步版本号 |

### `GET /api/sync/pull` 的查询参数

| 参数 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `cursor` | str \| null | 无 | 上次响应里的 `cursor`；首次同步不传 |
| `limit` | int \| null（≥1） | 无 | 每个数据流最多返回多少行，服务端还会按 `SYNC_PULL_MAX_ROWS` 截断 |
| `timeout` | float \| null（0..120） | 无 | 长轮询挂起上限（秒）；`0` 表示立即返回，不传用 `SYNC_LONG_POLL_SECONDS` |

首次同步（不带游标）与分页未拉完时都不挂起，直接返回。

## admin（`app/api/admin.py`，prefix `/api/admin`）

整个 router 挂在 `current_admin` 上：未登录 401，`role < admin` 403。只有改角色要求 `superadmin`。

### 提供商

| 方法 | 路径 | 鉴权 | 请求 → 响应 | 说明 |
|---|---|---|---|---|
| GET | `/api/admin/providers` | admin | — → `ProviderRead[]` | API Key 只回掩码 |
| GET | `/api/admin/provider-presets` | admin | — → `ProviderPresetRead[]` | 内置厂商预设目录，纯静态，不查库 |
| POST | `/api/admin/providers` | admin | `ProviderCreate` → 201 `ProviderRead` | 地址不合法或拉不到模型列表都返回 400 |
| PATCH | `/api/admin/providers/{provider_id}` | admin | `ProviderUpdate` → `ProviderRead` | 换 `base_url` 必须重填 `api_key`，否则 400 |
| DELETE | `/api/admin/providers/{provider_id}` | admin | — → 204 | 不存在 404，连带删除其模型 |
| POST | `/api/admin/providers/{provider_id}/test` | admin | — → `ProviderTestResult` | 测试失败是预期结果：200 + `ok=false`，原因在 `error` |
| POST | `/api/admin/providers/{provider_id}/sync` | admin | — → `ProviderSyncResult` | 上游故障 502，提供商不存在 404 |

### 模型

| 方法 | 路径 | 鉴权 | 请求 → 响应 | 说明 |
|---|---|---|---|---|
| GET | `/api/admin/providers/{provider_id}/models` | admin | — → `ModelRead[]` | 某提供商下的全部模型（含停用的） |
| POST | `/api/admin/providers/{provider_id}/models` | admin | `ModelCreate` → 201 `ModelRead` | 同提供商下重复 409；停用的模型设为默认 400；字段越界是 422 |
| PATCH | `/api/admin/models/{model_uuid}` | admin | `ModelUpdate` → `ModelRead` | 只传要改的字段；所属提供商不存在 409 |
| DELETE | `/api/admin/models/{model_uuid}` | admin | — → 204 | 不存在 404 |

### 用户

| 方法 | 路径 | 鉴权 | 请求 → 响应 | 说明 |
|---|---|---|---|---|
| GET | `/api/admin/users` | admin | query → `UserPage` | 搜索 / 筛选 / 分页，见下方参数表 |
| POST | `/api/admin/users/{user_id}/ban` | admin | — → `UserAdminRead` | 封自己、同级或超管都 400；连带删会话行并回收该用户沙箱机器 |
| POST | `/api/admin/users/{user_id}/unban` | admin | — → `UserAdminRead` | 等级校验与封禁完全对称 |
| PATCH | `/api/admin/users/{user_id}/role` | superadmin | `RoleUpdate` → `UserAdminRead` | 不能改自己、不能改已封禁账号、不能降级最后一个超管；成功后该用户全部会话失效 |

封禁等于 `is_active=False` 加删除该用户全部 `access_token` 行，所以是「立刻踢下线」，不是「下次登录才失效」。

`GET /api/admin/users` 的查询参数：

| 参数 | 类型 | 默认 | 含义 |
|---|---|---|---|
| `query` | str \| null（≤200） | 无 | 按邮箱模糊搜索，`%` 与 `_` 按字面匹配 |
| `status` | `all` \| `active` \| `banned` | `all` | 按账号状态筛选 |
| `limit` | int（1..200） | 50 | 每页条数 |
| `offset` | int（≥0） | 0 | 偏移 |

### `ProviderCreate`

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `name` | str（1..80） | 是 | 提供商显示名，不能为空白 |
| `base_url` | str \| null（≤500） | 否 | 必须以 `http://` 或 `https://` 开头；选了 `preset_key` 时可省 |
| `preset_key` | str \| null（≤40） | 否 | 内置预设的 key，用来填默认地址与认证方式 |
| `api_key` | str \| null（≤1000） | 否 | 上游密钥，只进不出 |
| `enabled` | bool（默认 `true`） | 否 | 是否启用 |

`base_url` 与 `preset_key` 至少要有一个，否则 422。

### `ProviderUpdate`

| 字段 | 类型 | 含义 |
|---|---|---|
| `name` | str \| null（1..80） | 不传或显式 `null` 都表示不修改 |
| `base_url` | str \| null（1..500） | 同上；改地址时 `api_key` 必须一起给 |
| `api_key` | str \| null（≤1000） | 不传 = 不修改，空串 = 清空，非空 = 更新 |
| `enabled` | bool \| null | 不传或 `null` 表示不修改 |

### `ProviderRead`

| 字段 | 类型 | 含义 |
|---|---|---|
| `id` | UUID | 提供商主键 |
| `name` | str | 显示名 |
| `slug` | str | 路由键前缀，模型的 `key` 由它拼出 |
| `base_url` | str | 接口地址 |
| `api_key_hint` | str \| null | 掩码后的密钥（如 `sk-…abcd`）；≤8 字符时只剩 `••••` |
| `enabled` | bool | 是否启用 |
| `model_count` | int | 该提供商下的模型数 |
| `created_at` | datetime | 创建时间 |
| `auth_style` | str | 密钥放进哪个头，见 `model_service.presets.AUTH_STYLES` |
| `preset_key` | str \| null | 创建时用的内置预设；自定义添加为 `null` |

### `ProviderPresetRead`

| 字段 | 类型 | 含义 |
|---|---|---|
| `key` | str | 预设标识，新增提供商时填进 `preset_key` |
| `name` | str | 厂商名 |
| `base_url` | str | 该厂商的接口地址 |
| `auth_style` | str | 认证方式 |
| `api_key_url` | str | 到哪申请密钥 |
| `needs_api_key` | bool | 是否必须有密钥（本机网关不需要） |
| `note` | str | 该厂商能同步到哪些元数据 |
| `group` | str | `overseas` / `cn` / `local` |

### `ModelCreate`

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `model_id` | str（1..200） | 是 | 上游模型 ID，不能为空白 |
| `display_name` | str \| null（≤200） | 否 | 展示名；空串由服务层回退成 `model_id` |
| `description` | str \| null（≤1000） | 否 | 说明 |
| `reasoning` | bool \| null | 否 | 是否支持思考 |
| `supported_reasoning_levels` | str[]（默认空） | 否 | 该模型接受的思考等级 |
| `context_window` | int \| null（1024..10000000） | 否 | 上下文窗口；下界是给固定开销留余量 |
| `max_output_tokens` | int \| null（1024..10000000） | 否 | 输出上限 |
| `enabled` | bool（默认 `true`） | 否 | 是否启用 |
| `is_default` | bool（默认 `false`） | 否 | 是否设为默认 |

### `ModelUpdate`

字段与 `ModelCreate` 相同但全部可选，且没有 `model_id`（上游 ID 不可改）。`display_name` 显式传 `null` 视为不修改。

### `ModelRead`

| 字段 | 类型 | 含义 |
|---|---|---|
| `id` | UUID | 模型行主键 |
| `provider_id` | UUID | 所属提供商 |
| `key` | str | 对外路由键 `{slug}/{model_id}`，对话时填进 `ChatRequest.model` |
| `model_id` | str | 上游模型 ID |
| `display_name` | str | 展示名 |
| `description` | str \| null | 说明 |
| `reasoning` | bool \| null | 是否支持思考 |
| `supported_reasoning_levels` | str[] | 思考等级 |
| `context_window` | int \| null | 上下文窗口 |
| `max_output_tokens` | int \| null | 输出上限 |
| `enabled` | bool | 是否启用 |
| `is_default` | bool | 是否默认 |
| `raw` | object \| null | 最近一次同步的上游原始条目；手动添加或从未同步为 `null` |

### `ProviderTestResult` / `ProviderSyncResult`

| 模型 | 字段 | 含义 |
|---|---|---|
| `ProviderTestResult` | `ok`（bool） | 能否拉到模型列表 |
| `ProviderTestResult` | `model_count`（int） | 拉到的模型数，失败时为 0 |
| `ProviderTestResult` | `error`（str \| null） | 失败原因 |
| `ProviderSyncResult` | `fetched`（int） | 上游返回的模型数 |
| `ProviderSyncResult` | `created`（int） | 新建的模型数 |
| `ProviderSyncResult` | `updated`（int） | 更新的模型数 |

### `UserAdminRead` / `UserPage`

`UserAdminRead` 与 `UserRead` 字段相同（`id`、`email`、`is_active`、`is_superuser`、`is_verified`、`role`）。

`UserPage`：`items`（`UserAdminRead[]`）、`total`（筛选后的总数）、`limit`、`offset`。

### `RoleUpdate`

| 字段 | 类型 | 必填 | 含义 |
|---|---|---|---|
| `role` | `"user"` \| `"admin"` | 是 | 目标角色。`superadmin` 不在允许值里：最高一档只能经 `scripts/seed_admin.py` 授予 |

## ops（`app/api/ops.py`，无前缀）

| 方法 | 路径 | 鉴权 | 请求 → 响应 | 说明 |
|---|---|---|---|---|
| GET | `/health` | 无 | — → `{"status": "ok", "version": "..."}` | 存活探针，不碰数据库与上游 |
| GET | `/health/ready` | 无 | — → 逐项状态 | 就绪探针；必需依赖失败 503，其余降级为 `degraded`（仍 200） |
| GET | `/api/metrics` | 无（默认关闭） | — → Prometheus 文本 | `METRICS_ENABLED=false` 时返回 404 |

三个端点都是 `include_in_schema=False`，不出现在 OpenAPI 里。`/health/ready` 逐项探测
`database` / `redis` / `storage` / `provider_keys`，只有 `READINESS_REQUIRED_DEPENDENCIES`（默认 `database`）
里的依赖失败才返回 503。`/api/metrics` 用 404 而不是 403 表示关闭：少一个免登录端点就少一分侦察面。

## 最小 curl 走查

以下命令假设后端跑在 `http://127.0.0.1:8000`，账号已存在。

### 1. 登录并保存 cookie

```bash
curl -i -c cookie.txt -X POST http://127.0.0.1:8000/api/auth/login \
  -d 'username=user@example.com' \
  -d 'password=<你的密码>'
```

期望 `204 No Content` 与一行 `Set-Cookie: bennett_session=...; HttpOnly; ...`。凭据错误是 400，
不是 401；发 `-H 'Content-Type: application/json'` 会 422。

### 2. 确认登录态

```bash
curl -s -b cookie.txt http://127.0.0.1:8000/api/auth/me
```

返回形如 `{"id":"...","email":"user@example.com","is_active":true,"is_superuser":false,"is_verified":false,"role":"user"}`。

### 3. 发一轮对话（SSE）

```bash
curl -N -b cookie.txt -X POST http://127.0.0.1:8000/api/chat \
  -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"用一句话说明什么是 SSE"}]}'
```

`-N` 关掉 curl 自己的缓冲，否则看不到逐字输出。响应是一串帧，形如：

```text
data: {"type":"thinking","delta":"..."}

data: {"type":"content","delta":"SSE"}

data: {"type":"done"}
```

模型清单来自 `GET /api/models`，把其中的 `id` 填进 `model` 字段即可指定模型；不填用后台默认。

### 4. 推一条会话到同步

```bash
curl -s -b cookie.txt -X POST http://127.0.0.1:8000/api/sync/push \
  -H 'Content-Type: application/json' \
  -d '{"conversations":[{"id":"sess_demo","title":"第一轮","created_at":1767225600000,"updated_at":1767225600000}]}'
```

返回形如 `{"version":1,"accepted":{"conversations":1,"messages":0},"rejected":[]}`。`id` 必须匹配
`[A-Za-z0-9_-]{1,64}`；`created_at` 与 `updated_at` 是必填的毫秒时间戳。

### 5. 按游标拉回增量

```bash
curl -s -b cookie.txt 'http://127.0.0.1:8000/api/sync/pull?cursor=v1:0'
```

首次同步可以不传 `cursor`。响应里的 `cursor` 原样回传下一次；`has_more` 为真时继续拉。

## 相关源码

- `app/api/auth.py` — 认证端点、注册开关、改密限流
- `app/api/chat.py` — 模型列表、SSE 对话、模型与思考等级校验
- `app/api/capabilities.py` — 能力探针
- `app/api/sandbox.py` — 心跳、状态、产物
- `app/api/sync.py` — push / pull 与游标错误映射
- `app/api/admin.py` — 提供商、模型、用户、角色
- `app/api/ops.py` — 健康检查与指标
- `app/api/rate_limit.py` — 限流器与 429 响应头
- `app/schemas/` — 上面全部请求/响应模型
- `app/services/auth.py` — `current_active_user` / `current_admin` / `current_superadmin`
- `app/server.py` — router 装配、OpenAPI 与公开端点集合
- `app/config/settings.py` — 限流配额、同步上限、cookie 名的默认值
