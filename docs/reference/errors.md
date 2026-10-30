---
title: 错误码与失败模式
description: 按来源分组的错误目录：触发条件、状态码、错误标识、该改什么，每条都标注源码依据。
---

# 错误码与失败模式

这页是排障用的目录：一条错误发生时，先按来源定位它由谁抛出，再看错误标识与处理动作。
每条都标了源码位置，改动代码后可以直接回来核对。

接口面的契约（端点、请求字段、SSE 事件）见 [API 与 SSE 契约](../design/api.md)；
按症状组织的排查步骤见 [故障排查](../ops/troubleshooting.md)。本页只回答「这个 4xx 是哪来的、该改什么」。

## 先分清两条通道

| 通道 | 何时发生 | 形态 | 谁能改 |
|---|---|---|---|
| HTTP 错误响应 | 响应头还没发出：认证、限流、请求体校验、模型白名单、同步游标 | 4xx/5xx + JSON `detail` | 客户端改请求，或运维改配置 |
| SSE `error` 事件 | 已经回了 200 `text/event-stream`：上下文超限、上游失败、超时 | `data: {"type":"error",...}` | 换参数、换模型，或运维修上游 |

分界线是响应头。`POST /api/chat` 一旦开始拉事件流，状态码就改不了了——预检失败也必须在流里表达，
否则客户端只会看到「200 + 断流」，连错误编号都拿不到（`app/services/chat_service.py:286-289`）。

`detail` 有三种形状，前端按形状分派而不是按状态码猜：

- 纯中文句子：直接展示给用户（多数 4xx/5xx）。
- `CODE_原因`：冒号前是稳定错误码，冒号后是可变原因。例如 `REGISTER_INVALID_PASSWORD: 密码至少需要 8 个字符`。
- `中文句子（code=xxx）`：同步接口的写法，`code` 是给程序判断的（`app/api/sync.py:59-62`）。

500 响应体额外带 `error_id`，与响应头 `X-Request-ID` 同值（`app/server.py:303-325`）。

## 认证与权限

| 触发条件 | 状态码 | 错误标识（`detail`） | 处理 | 依据 |
|---|---|---|---|---|
| 没有 cookie、cookie 对应会话已过期，或账号被封禁 | 401 | `Unauthorized`，带 `WWW-Authenticate: Bearer` | 重新登录；前端收到 401 即退回登录页 | `app/services/auth.py:186-196` |
| 已登录但 `role < admin`，访问 `/api/admin/*` | 403 | `需要管理员权限` | 由 superadmin 提权，或 `scripts/seed_admin.py` 播种 | `app/services/auth.py:199-206` |
| 非 superadmin 调 `PATCH /api/admin/users/{id}/role` | 403 | `需要超级管理员权限（只有超级管理员可以调整角色）` | 换 superadmin 操作 | `app/services/auth.py:209-216` |
| `REGISTRATION_ENABLED=false` 时调注册 | 403 | `注册功能已关闭` | 找管理员建号 | `app/api/auth.py:76-81` |
| 注册密码短于 8 位 | 400 | `REGISTER_INVALID_PASSWORD: 密码至少需要 8 个字符` | 改密码后重试 | `app/api/auth.py:107-115`、`app/services/auth.py:121-123` |
| 改密时当前密码填错 | 400 | `CHANGE_PASSWORD_BAD_CREDENTIALS` | 重新输入当前密码；**不要**当成会话过期去登出 | `app/api/auth.py:176-181` |
| 改密时新旧密码相同 | 400 | `CHANGE_PASSWORD_SAME` | 换一个新密码 | `app/api/auth.py:184-187` |
| 新密码不合规 | 400 | `CHANGE_PASSWORD_INVALID: <原因>` | 按原因改 | `app/api/auth.py:193-196` |
| 浏览器跨站发起 `/api/*` 状态变更请求 | 403 | `跨站请求被拒绝` | 把前端来源加进 `CORS_ALLOWED_ORIGINS` | `app/middleware.py:128-131`、`app/middleware.py:134-186` |

两处刻意的状态码选择，改代码时别顺手「修正」：

- 改密填错当前密码是 400 不是 401。401 会被前端的全局处理当成会话过期直接登出，
  而这里只是输错了当前密码（`app/api/auth.py:176-181`）。
- 登录凭据错误是 400、没有登录态是 401。登录接口的 `detail` 由 fastapi-users 生成，
  本仓库不改写它（`app/api/auth.py:1-6` 的模块说明）。

## 请求校验

| 触发条件 | 状态码 | 错误标识（`detail`） | 处理 | 依据 |
|---|---|---|---|---|
| 请求体超过 `MAX_REQUEST_BODY_BYTES` | 413 | `请求体过大，已拒绝` | 分批发送；不要上传附件 | `app/middleware.py:30-44`、`app/middleware.py:45-95` |
| 请求体不满足 Pydantic 模型（含历史顺序、长度、审批形状） | 422 | `detail` 是逐字段数组（`loc` / `msg` / `type`） | 按 `loc` 指到的字段改请求 | `app/schemas/chat.py` |
| 显式传了 `model` 但不在启用列表里 | 400 | `模型 {model} 不在可用模型列表中，请从模型列表中选择。` | 刷新 `/api/models` 后重选 | `app/api/chat.py:88-95` |
| 请求的思考等级不在该模型声明的等级内 | 400 | `模型 {key} 不支持思考等级 {effort}，可选等级：<列表>。` | 从列表里选，或不传该字段 | `app/api/chat.py:100-118` |
| 后台一个启用的模型都没有 | 503 | `管理员尚未启用任何模型，请联系管理员。` | 运维到管理后台启用模型并设默认 | `app/api/chat.py:96-98` |

422 里最常见的几条（文案来自模型校验器，会原样出现在 `msg` 里）：

| 校验 | 触发条件 | 依据 |
|---|---|---|
| 首条必须是 user | 历史以 assistant 开头 | `app/schemas/chat.py:211-216` |
| 必须交替 | 相邻两条同角色 | `app/schemas/chat.py:218-223` |
| 末条必须是 user | 普通请求以 assistant 结尾（续跑审批除外） | `app/schemas/chat.py:225-230` |
| 历史总量上限 | `content + thinking` 合计超过 400000 字符 | `app/schemas/chat.py:248-254` |
| `tool_calls` 只能挂 assistant | user 消息带了 `tool_calls` | `app/schemas/chat.py:105-112` |
| 审批 id 必须在历史里 | `approvals` 里出现历史中没有的 `tool_call_id` | `app/schemas/chat.py:196-205` |
| 只接受纯文本 | `content` 分片里出现 `image` / `file` | `app/schemas/chat.py:55-63` |
| `session_id` 形状 | 不匹配 `^sess_[A-Za-z0-9_-]{1,59}$` | `app/schemas/chat.py:18-20` |

`session_id` 的字符集必须与 `app/schemas/sync.py:13` 的 `ID_PATTERN` 一致：两处一旦分叉，表现是
「同步能存下的会话，聊天一发就 422」。

## 限流

| 触发条件 | 状态码 | 错误标识 | 处理 | 依据 |
|---|---|---|---|---|
| 某个桶超过每分钟配额 | 429 | `请求过于频繁，请在 {N} 秒后重试` | 等到 `Retry-After` 秒后再发，不要盲目重试 | `app/api/rate_limit.py:349-370` |
| Redis 不可用且 `RATE_LIMIT_ON_REDIS_ERROR=fail_closed` | 429 | 同上，`N` 取整个窗口（60 秒） | 修 Redis；这是配置选定的「牺牲可用性换确定性」 | `app/api/rate_limit.py:290-303` |

429 一定带 `Retry-After`（向上取整、最小 1 秒）；调用方显式传了配额时还带
`X-RateLimit-Limit`、`X-RateLimit-Remaining: 0`、`X-RateLimit-Reset`（`app/api/rate_limit.py:349-370`）。
这几个头由 `app/server.py` 的 `expose_headers` 放行，否则浏览器读不到。

四个桶互相独立，配额配成 `0` 表示该桶关闭（依赖仍然生效，登录要求不会因此消失）：

| 桶 | 键 | 配额字段 | 端点 |
|---|---|---|---|
| 认证 | `来源 IP` + 路径末段 | `auth_rate_limit_per_minute` | `/api/auth/login`、`/api/auth/register` |
| 对话 | `user:<id>` | `chat_rate_limit_per_minute` | `/api/chat`、`/api/chat/title` |
| 改密 | `user:<id>` | `change_password_rate_limit_per_minute` | `/api/auth/change-password` |
| 同步 | `user:<id>` | `sync_rate_limit_per_minute` | `/api/sync/*` |

认证桶按 IP 而不是账号，是因为登录前没有账号身份；反向代理下如果没同时配
`TRUSTED_PROXY_HEADERS` 与 `TRUSTED_PROXY_IPS`，所有请求会被算成同一个来源，全站共享配额
（`app/server.py:_warn_if_auth_rate_limit_is_global`）。

## 模型网关

### 管理后台的提供商与模型接口

| 触发条件 | 状态码 | 错误标识（`detail`） | 处理 | 依据 |
|---|---|---|---|---|
| 提供商 id 不存在 | 404 | `提供商不存在` | 刷新列表 | `app/api/admin.py:84-88`、`app/api/admin.py:156-158`、`app/api/admin.py:167-170` |
| Base URL 指向云元数据 / 链路本地地址 | 400 | `Base URL 不允许指向链路本地或未指定地址（<ip>）` | 换地址；这是 SSRF 防护，没有开关 | `app/services/model_service/outbound_guard.py:128-132` |
| `PROVIDER_BLOCK_PRIVATE_TARGETS` 开启且指向内网 | 400 | `Base URL 不允许指向内网地址（<ip>）` | 换地址，或关掉该严格模式（本机网关场景） | `app/services/model_service/outbound_guard.py:132` |
| 没填 Base URL 也没选预设 | 400 | `必须填写 Base URL，或选择一个厂商预设` | 补字段 | `app/services/model_service/provider_sync.py:101` |
| 改了 Base URL 但没重填 API Key | 400 | `更换 Base URL 时必须重新填写 API Key` | 重填密钥：旧 key 不能被送到新地址 | `app/api/admin.py:150-154`、`app/services/model_service/provider_sync.py:173` |
| 同一提供商下已有同名模型 | 409 | `该模型已存在` | 改 `model_id` 或改已有那条 | `app/api/admin.py:238-241` |
| 把已停用的模型设为默认 | 400 | `停用的模型不能设为默认模型` | 先启用再设默认 | `app/api/admin.py:242-243`、`app/services/model_service/provider_sync.py:241` |
| 模型 id 不存在 | 404 | `模型不存在` | 刷新列表 | `app/api/admin.py:255-259`、`app/api/admin.py:270-274` |
| 模型所属提供商已被删 | 409 | `模型所属提供商不存在` | 重建提供商或删掉该模型 | `app/api/admin.py:260-263` |
| 同步模型列表时上游不可达 / 返回非 JSON / 结构不符 | 502 | `str(ProviderFetchError)`，如 `提供商返回的不是 JSON` | 查 Base URL 与上游网关 | `app/api/admin.py:194-201`、`app/services/model_service/provider_sync.py:349-370` |
| 连通性测试失败 | **200** | 响应体 `ok=false` + `error` 文案 | 界面把原因显示在按钮旁；失败是预期结果，不是 HTTP 错误 | `app/api/admin.py:178-188` |

`/providers/{id}/test` 刻意返回 200：把「测试失败」做成 502 会让管理员以为网关挂了，
而它本来就是一个「测一下」的动作。

### 用户与角色

| 触发条件 | 状态码 | 错误标识（`detail`） | 依据 |
|---|---|---|---|
| 用户 id 不存在 | 404 | `用户不存在` | `app/api/admin.py:343-346` |
| 封禁自己 | 400 | `不能封禁当前登录的管理员账号` | `app/api/admin.py:339-342` |
| 封禁同级或更高级 | 400 | `不能封禁超级管理员` / `不能封禁同级管理员` | `app/api/admin.py:347-351` |
| 解封同级或更高级 | 400 | `不能解封超级管理员` / `不能解封同级管理员` | `app/api/admin.py:382-386` |
| 修改自己的角色 | 400 | `不能修改自己的角色` | `app/api/admin.py:409-412` |
| 给已封禁账号改角色 | 400 | `该账号已被封禁，请先解封再修改角色` | `app/api/admin.py:416-418` |
| 降级最后一个 superadmin | 400 | `不能降级最后一个超级管理员，后台将无人可管` | `app/api/admin.py:436-445` |

### 上游错误的中文映射

上游异常原文永不外泄，只按状态码映射成一句话（`app/services/upstream_errors.py:11-20`）：

| 上游状态 | 用户看到的文案 |
|---|---|
| 400 / 404 | `模型不可用，请更换模型` |
| 401 / 403 | `上游认证配置问题，请联系管理员` |
| 429 | `上游限流，请稍后重试` |
| 5xx | `服务暂时不可用，请重试` |
| 超时（异常链里出现 `TimeoutError` / `httpx.TimeoutException`） | `服务暂时不可用，请重试` |
| 其它（含连不上、SDK 包装后的无状态码异常） | `生成异常，请重试` |

这份映射同时用于 SSE `error` 事件与标题生成；判定「能不能自动重试 / 能不能切备用模型」也读同一份，
两处漂移会让容灾行为与文案互相矛盾（`app/services/upstream_errors.py:1-5`）。

## 工具执行

工具层的失败不抛给用户，而是变成 `tool_result` 事件（`outcome` 非 `success`）或一句回灌给模型的文案。
模型看到文案后自行换策略，整轮对话继续。

| 触发条件 | 出现位置 | 文案 / 标识 | 模型该做什么 | 依据 |
|---|---|---|---|---|
| 工具抛出未预期异常 | `tool_result`，`outcome=failed` | `工具执行失败（内部错误），本次没有取得结果。不要原样重复调用同一个工具…` | 换一种方式，或如实告知用户该步失败 | `app/runtime/guard.py:24-28`、`app/runtime/guard.py:118-122` |
| 同一工具本轮累计失败到第 2 次 | 同上，文案升级 | `工具 X 本轮已连续失败 N 次（内部错误），继续调用它不会成功。请立即停止调用…` | 停止调用该工具 | `app/runtime/guard.py:30`、`app/runtime/guard.py:84-89` |
| 网络抖动 / 超时（`httpx.TransportError`、`TimeoutError`） | 同上 | `工具暂时失败（网络或上游超时）…可以再试一次；若仍然失败…` | 可重试一次，不升级成「停止调用」 | `app/runtime/guard.py:33-45` |
| 参数校验失败（`RetryPromptPart`） | `tool_result`，`outcome=failed` | 结果文本即校验错误 | 按错误改参数重发 | `app/services/event_translation.py:83-91` |
| 用户拒绝了审批 | `tool_result`，`outcome=denied` | `用户拒绝执行这次工具调用，它没有被执行。不要重试同一个调用…` | 不要重试，向用户说明并询问下一步 | `app/runtime/approval.py:32-36` |
| 工具需要审批但还没拿到批准 | `approval_required` 事件 | 本轮以审批请求结束，**不是错误** | 客户端回传 `approvals` 续跑 | `app/runtime/approval.py:68-92` |
| 需要审批的调用没有审批记录（`tool_call_approved` 不是 `True`） | 同上 | 一律拦下 | 同上 | `app/runtime/approval.py:76-90` |

内置工具自己也会返回「错误文案」而不是抛异常，这些文案同样只给模型看：

| 工具 | 触发条件 | 返回的文案 | 依据 |
|---|---|---|---|
| `tavily_search` | 未配置 API Key | `错误: 未配置 Tavily API Key，无法执行联网搜索。` | `app/tools/tavily_search.py:81-84` |
| `tavily_search` | 关键词为空 | `错误: 搜索关键词为空，请提供具体要检索的内容后再调用本工具。` | `app/tools/tavily_search.py:86-90` |
| `tavily_search` | 上游 401 | `联网搜索认证失败 (HTTP 401): Tavily API Key 无效或未授权。` | `app/tools/tavily_search.py:185-190` |
| `tavily_search` | 上游 429 | `联网搜索请求过于频繁或配额耗尽 (HTTP 429)…` | `app/tools/tavily_search.py:192-196` |
| `tavily_search` | 请求超时 / 连接异常 | `联网搜索超时，关键词: <q>` / `联网搜索发生异常，关键词: <q>` | `app/tools/tavily_search.py:118-127` |
| `tavily_search` | 响应不是 JSON 或结构不符 | `联网搜索响应解析失败…` / `联网搜索返回了非预期的数据结构…` | `app/tools/tavily_search.py:133-146`、`app/tools/tavily_search.py:204-212` |
| `ask_user_clarification` | `question` 为空 / `options` 不是列表 | `错误: 提问内容 (question) 不能为空。` / `错误: 候选项列表 (options) 必须为列表或元组。` | `app/runtime/clarification.py:36-46` |

工具失败的计数是本轮内的（`ContextVar`，并发请求各持一份）：换一轮对话后重新从第 1 次算起
（`app/runtime/guard.py:66-72`）。

## 沙箱

### 客户端端点

| 触发条件 | 状态码 | 错误标识（`detail`） | 处理 | 依据 |
|---|---|---|---|---|
| `SANDBOX_ENABLED=false` 时调 `/status` 或 `/artifacts` | 404 | `未启用执行环境` | 部署没开沙箱；前端据此不显示「我的电脑」 | `app/api/sandbox.py:85-88`、`app/api/sandbox.py:119-122` |
| 该用户从未有过租约 | 404 | `该会话还没有执行环境` | 正常状态，不是故障：执行一次工具才会建机 | `app/api/sandbox.py:89-92` |
| 心跳写入 Redis 失败 | **204** | 无 | 只记 WARNING，下一轮再试；心跳失败不该在界面上冒错 | `app/api/sandbox.py:57-80` |

`/artifacts` 永远 200（除未启用外）：「还没销毁过所以没产物」是最常见的情形，做成 404 会让每次轮询
都变成一条错误日志（`app/api/sandbox.py:104-130`）。

### 工具层文案

沙箱工具对模型永远返回文案、不抛异常；四种失败对应四种下一步动作，不能合并成一句
（`app/tools/sandbox.py:37-40`、`app/tools/sandbox.py:158-170`）：

| 触发条件 | 文案 | 模型下一步 | 依据 |
|---|---|---|---|
| 并发/池已满（`SandboxCapacityError`） | `当前执行环境资源已满，请稍后再试。` | 等一会儿再试 | `app/tools/sandbox.py:37` |
| 机器不存在或已回收（`SandboxNotFound`） | `执行环境不存在或已回收，请重试；下次执行会自动创建新环境。` | 直接重试（会重建） | `app/tools/sandbox.py:38` |
| 命令超时（`SandboxTimeout`） | `命令超时（可调大 timeout_s 或缩小范围）。` | 缩小范围或调大超时，**不要**重建环境 | `app/tools/sandbox.py:39` |
| 其它 `SandboxError` / 未预期异常 | `执行环境暂时不可用，请稍后再试。` | 稍后重试 | `app/tools/sandbox.py:40` |
| 请求没有用户作用域 | `当前会话没有关联用户（未登录），无法使用执行环境…` | 说明没有执行环境 | `app/tools/sandbox.py:32-35` |
| 租约还在 `provisioning` | `执行环境正在启动，请稍后再试。` | 等几秒 | `app/tools/sandbox.py:100-108` |
| 路径不在工作目录下或含 `..` | `路径不可用：…只允许执行环境工作目录 <workdir> 下的绝对路径…` | 改用工作目录下的绝对路径 | `app/tools/sandbox.py:131-139` |
| 单次写入超过 200000 字符 | `内容 N 字符超过单次写入上限…请拆成多个文件或分段写入…` | 分段写 | `app/tools/sandbox.py:43`、`app/tools/sandbox.py:239-245` |
| 重置时机器正在创建/回收 | `当前执行环境正在创建或回收中，本次没有执行重置…` | 稍后重试 | `app/tools/sandbox.py:322-324` |
| 命令为空 | `命令为空，没有执行。请给出要执行的具体命令。` | 给具体命令 | `app/tools/sandbox.py:185-188` |

`SandboxNotFound` 的文案刻意不区分「不存在」与「不属于你」：能区分就成了存在性探测器
（`app/services/sandbox/base.py:27-31`、`app/tools/sandbox.py:158-170`）。

provider 层的配置错误只进日志，不会漏给模型：`SANDBOX_SERVER_URL` 为空时抛
`未配置 SANDBOX_SERVER_URL：沙箱服务地址为空。`（`app/services/sandbox/opensandbox.py:64-70`），
工具层接住后统一回 `执行环境暂时不可用，请稍后再试。`。排查时按日志里的
`沙箱命令执行失败 (user=…)` 找异常类型名。

## 同步

| 触发条件 | 状态码 | 错误标识（`detail`） | 处理 | 依据 |
|---|---|---|---|---|
| 单次 push 行数超过 `SYNC_PUSH_MAX_ROWS` | 400 | `<原因>（code=too_many_rows）` | 客户端分批推 | `app/api/sync.py:57-63`、`app/services/sync_service.py:60-61` |
| 游标格式不认识（缺前缀、非整数、为负） | 400 | `<原因>（code=invalid_cursor）` | 丢弃游标，全量重拉 | `app/api/sync.py:83-89`、`app/services/sync_service.py:52-53`、`app/services/sync_service.py:74-86` |
| 游标版本超过服务端当前版本（库被回滚/重建过） | 410 | `<原因>（code=resync_required）` | 丢弃游标，全量重拉；与 400 对客户端动作相同 | `app/api/sync.py:103-109`、`app/services/sync_service.py:56-57` |
| 请求体多传字段、id 形状不符、`client_rev` 或时间戳越界 | 422 | Pydantic 逐字段错误 | 修请求；`extra="forbid"` 是刻意的，逼双方同时升级 | `app/schemas/sync.py:13-52` |

行级拒绝不走 HTTP 状态码：整批照常 200，`rejected[]` 里逐行给 `reason`
（`not_owned` / `stale` / `too_large` / `conversation_missing` / `conversation_deleted` / `quota_exceeded`）。
每个原因该丢弃还是重试，见 [跨设备同步](../features/sync.md) 的「拒绝原因与客户端动作」。

## 服务端内部

| 触发条件 | 状态码 | 错误标识 | 处理 | 依据 |
|---|---|---|---|---|
| 任何未捕获异常 | 500 | `服务器内部错误，请重试（错误编号 <id>）`，响应体另有 `error_id` | 拿 `error_id` / `X-Request-ID` 去日志里查 `未处理异常 (error_id=…)` | `app/server.py:303-325` |
| `/health/ready` 的必需依赖（默认 `database`）不可用 | 503 | `{"status":"not_ready", ...}`，每项带 `status` / `reason` | 看是哪一项：`timeout` / `unavailable` / `not_configured` | `app/api/ops.py:62-95` |
| `METRICS_ENABLED=false` 时调 `/api/metrics` | 404 | 空 body | 这是默认值，不是故障；需要时显式打开并挡在内网 | `app/api/ops.py:136-142` |

进程启动阶段的失败不是 HTTP 错误（服务根本不会起来），日志关键字与处理见
[故障排查](../ops/troubleshooting.md) 的「启动与配置」：提示词自检（`app/server.py:78-95`）、
生产环境守卫（`app/config/settings.py:780-795`）、CORS 组合校验（`app/server.py:350-356`）。

## SSE 流内的错误

### 为什么不能回 4xx

`POST /api/chat` 先发响应头再拉第一个事件。预检异常不能突破生成器，否则 200 已不可撤回，
客户端只会看到「200 + 断流」，连错误编号都留不下（`app/services/chat_service.py:286-289`）。
所以**所有**发生在流开始之后的失败都走 `error` 事件：

```json
{"type":"error","message":"生成超时，请重试","retryable":true,"code":null}
```

`code` 只有三个取值：`context_overflow`、`model_unavailable`、`internal_error`；多数失败是 `null`
（`app/schemas/events.py:149-158`）。`retryable` 只决定前端是否显示重试按钮，不代表服务端会自动重试。

### 全部 `error` 事件

| 触发条件 | `message` | `retryable` | `code` | 依据 |
|---|---|---|---|---|
| 库里没有该模型或已停用 | `模型已停用或不存在，请刷新模型列表后重试` | `false` | `model_unavailable` | `app/services/chat_service.py:296-303` |
| 模型预检本身失败（读库/预算解析异常） | `请求处理失败，请重试（错误编号 <id>）` | `true` | `internal_error` | `app/services/chat_service.py:317-324` |
| 上下文超出模型预算且压缩后仍超 | `本会话上下文约 N tokens，超出模型 X 的可用上限 M（该模型窗口 W）。请新建会话…` | `false` | `context_overflow` | `app/services/chat_service.py:343-351`、`app/services/token_budget.py:346-356` |
| 历史形状非法（首条/交替/末条） | 校验器原文，可直接展示 | `false` | `null` | `app/services/chat_service.py:387-392`、`app/services/chat_history.py:26-30` |
| 历史转换出现意外异常 | `请求处理失败，请重试（错误编号 <id>）` | `true` | `null` | `app/services/chat_service.py:393-398` |
| 构造 Agent 失败 | `模型初始化失败，请重试或切换模型（错误编号 <id>）` | `true` | `null` | `app/services/chat_service.py:405-410` |
| 整轮超过 `TOTAL_STREAM_BUDGET_SECONDS`（600 秒） | `生成超时，请重试` | `true` | `null` | `app/services/chat_service.py:533-538`、`app/services/chat_service.py:100` |
| 上游异常（含备用模型全失败） | `<上游映射文案>（错误编号 <id>）` | 由 `is_retryable_for_user` 判定 | `null` | `app/services/chat_service.py:552-559` |

`context_overflow` 的文案必须带用量、上限、窗口三个数字与两个动作：消息已落库，下一轮只会更长，
没有数字用户不知道删多少（`app/services/token_budget.py:346-356`）。

### 不是错误的三件事

| 现象 | 为什么不算错误 | 依据 |
|---|---|---|
| 客户端断开，流直接结束、没有 `error` 帧 | `CancelledError` 原样上抛，往已断开的连接写帧无意义；统计记为 `stopped` 而不是失败 | `app/services/chat_service.py:524-528` |
| 本轮以 `approval_required` 结束 | 审批是正常分段，事件之后仍会发 `done`；统计单独记 `approval_required` | `app/services/chat_service.py:484-506`、`app/services/chat_service.py:564` |
| 收到 `fallback` 事件 | 主模型失败但备用模型接住了，本轮成功；`reason` 说明为什么切 | `app/schemas/events.py:55-65` |

半开连接是另一类「看起来像断流」的故障：单次 ASGI send 有 30 秒超时
（`SSE_SEND_TIMEOUT_SECONDS`），超时会中断整轮，而不是无限挂着占上游额度（`app/api/chat.py:32-35`）。

## 重试语义

### 服务端自己会重试的

| 场景 | 条件 | 次数与退避 | 依据 |
|---|---|---|---|
| 流式生成 | **尚未向客户端产出任何事件**，且异常是 5xx / 超时 / 连接类 | 首次 + 1 次，退避 0.5 秒 | `app/services/chat_service.py:98-105`、`app/services/chat_service.py:539-551` |
| 切换到备用模型 | 429 或 5xx / 超时 / 连接类 | 由 `FallbackModel` 触发一次，切完继续本轮 | `app/services/upstream_errors.py:82-89`、`app/services/agent_factory.py:196-223` |
| `tavily_search` 内部 | 运输层错误与 5xx | 首次 + 1 次，退避 0.5 秒 | `app/tools/tavily_search.py:37-40`、`app/tools/tavily_search.py:148-182` |
| 同步 push 撞并发主键 | `IntegrityError` | 整批重放一次 | `app/services/sync_service.py:338-348` |
| 沙箱回收 | 销毁失败 | 落 `destroy_failed`，下一轮回收重新抢它 | `app/services/sandbox/reaper.py:160-180` |

已经产出过事件就不再重试：否则正文重复，有副作用的工具会跑两次
（`app/services/chat_service.py:98-101`）。

### 值得重试的

| 错误 | 理由 |
|---|---|
| 429（带 `Retry-After`） | 窗口会滑过去，等到点再发就行 |
| 5xx、`服务暂时不可用，请重试` | 上游瞬时故障 |
| `生成超时，请重试` | 总预算耗尽，通常与上游慢有关 |
| `error_id` 结尾的 `internal_error` | 服务端自身异常，可能是一次抖动；带上 `error_id` 报障 |
| 工具文案里的「网络或上游超时」「可以再试一次」 | `transient` 判定，未升级成「停止调用」 |
| 沙箱 `执行环境不存在或已回收` / `正在启动` / `资源已满` | 分别会重建、会就绪、会腾出容量 |
| 同步 `conversation_missing` | 会话还没推上去，下一批先推会话即可 |

### 换参数才有用的

| 错误 | 该改什么 |
|---|---|
| 400 `模型 … 不在可用模型列表中` | 重新拉 `/api/models`，用返回的 id |
| 400 `不支持思考等级 …` | 从文案给出的可选等级里选 |
| 422 任意字段 | 按 `loc` 改字段；历史顺序、长度、审批 id 都属于这类 |
| 413 `请求体过大` | 拆批、缩短历史 |
| `context_overflow` | 新建会话或换窗口更大的模型（同一个请求再发一次结果不变） |
| 400 `更换 Base URL 时必须重新填写 API Key` | 重填密钥 |
| 400 `停用的模型不能设为默认模型` | 先启用 |
| 沙箱 `路径不可用` / `命令超时` | 改用工作目录下的绝对路径 / 缩小范围或调大 `timeout_s` |

### 重试只会更糟的

| 错误 | 为什么 |
|---|---|
| 401 / 403 | 没有登录态或权限不够，重发同一个请求结果一样；该做的是重新登录或提权 |
| 400 `CHANGE_PASSWORD_*` | 输入问题；反复试还会撞改密限流桶 |
| 409 `该模型已存在` | 每次重试都是一次注定失败的写 |
| `model_unavailable` | 配置里没有该模型，重试不会把它变出来 |
| 工具文案「用户拒绝执行」「请立即停止调用」 | 模型被明确告知不要重试；再调会再弹一次审批框或再烧一次 token |
| 同步 `stale` / `not_owned` / `conversation_deleted` | 永远不会成功；客户端应直接丢弃 |
| 同步 `too_large` / `quota_exceeded` | 要用户删内容，重试只是重复拒绝 |

## 相关源码

- `app/services/auth.py`、`app/api/auth.py` — 认证依赖、注册/改密错误码
- `app/middleware.py` — 413 与跨站 403
- `app/api/rate_limit.py` — 429、限流头与 Redis 降级
- `app/api/chat.py`、`app/services/chat_service.py` — 模型白名单、SSE 错误事件、重试循环
- `app/services/upstream_errors.py` — 上游文案与三类重试判定
- `app/runtime/guard.py`、`app/runtime/approval.py` — 工具失败隔离与审批拒绝
- `app/tools/sandbox.py`、`app/services/sandbox/base.py` — 沙箱失败文案与异常分类
- `app/api/sync.py`、`app/services/sync_service.py` — 同步游标错误与行级拒绝
- `app/server.py` — 未捕获异常处理与 `error_id`
