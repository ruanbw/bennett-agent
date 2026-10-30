---
title: 一轮对话的链路
description: 从 POST /api/chat 到 SSE 结束的一轮完整时序，含失败重试路径与三处预算。
---

# 一轮对话的链路

一轮对话在服务端是**一个生成器**：请求进来后先做完所有能提前判定的检查，再边生成边产出
类型化事件，最后以 `done` 或 `error` 收尾。本页按时序走一遍，标出每一步的判定点、失败路径
与时间预算。

相关源码集中在三个文件：`app/api/chat.py`（入口与序列化）、`app/services/chat_service.py`
（编排）、`app/services/agent_factory.py`（装配）。

## 链路总览

```text
客户端
  │ POST /api/chat {model, reasoning_effort, messages, session_id, approvals}
  ▼
[1] 中间件（由外到内）：安全头 → 请求 ID → 指标 → CORS → Origin 守卫(403) → 请求体上限(413)
  ▼
[2] Cookie 认证：current_active_user → 无效/停用 401
  ▼
[3] 账号限流：user:<id> 每分钟 N 次 → 429 + Retry-After
  ▼
[4] 模型快照 resolve_chat_model：白名单 + 目标 + 窗口上限（一次读库）
  │     ├─ 显式 model 不在白名单 → 400
  │     └─ 库已接管但无启用模型 → 503
  ▼
[5] reasoning_effort 校验：不在该模型声明的等级内 → 400
  ▼
[6] 预算判定 resolve_context_budget → 超限则压缩 → 仍超限则 error(context_overflow)
  ▼
[7] 历史转换 convert_chat_history → 非法序列 → error（无 code，retryable=false）
  ▼
[8] Agent 装配 build_agent（lru_cache）→ 失败 → error（无 code，retryable=true）
  ▼
[9] 流式生成 agent.run_stream_events
  │     ├─ 工具调用：作用域注入 → 审批闸门 → 异常隔离
  │     ├─ 审批拦下 → usage → approval_required → 本轮结束
  │     └─ 上游失败 → 切备用模型 → 瞬时错误重试一次
  ▼
[10] usage → done
  ▼
[11] SSE 分帧（app/api/chat.py: to_sse_frame）
  ▼
客户端落库（IndexedDB）→ push 到服务端 → 其他设备 pull
```

## 逐步说明

### [1] 中间件

请求先过 `app/middleware.py`：`MaxBodySizeMiddleware` 按 `Content-Length` 预检、
按实际读取字节数兜底，超限返回 413；`OriginGuardMiddleware` 拒绝跨站的状态变更请求；
`SecurityHeadersMiddleware` 给所有响应加 `nosniff` / `X-Frame-Options` / `Referrer-Policy`，
并额外给 `/api/*` 加 `Cache-Control: no-store`。
`app/server.py` 的注册顺序是「请求体上限 → Origin 守卫 → CORS → 指标 → 请求 ID → 安全头」，
而 FastAPI/Starlette 把**最后注册的放在最外层**，所以实际执行顺序是倒序：安全头 → 请求 ID →
指标 → CORS → Origin 守卫 → 请求体上限。把 413 与 403 两个中间件放在 CORS 之前注册是刻意的：
它们在外层中间件之内、CORS 之外，响应才带 CORS 头，浏览器里的前端才读得到错误。

### [2] Cookie 认证

`current_active_user`（`app/services/auth.py`）从 HttpOnly cookie 取会话 token，查
`accesstoken` 表（库里存的是 SHA-256 摘要），未登录或 `is_active=False` 返回 401。
它刻意不声明数据库 `Depends`：SSE 与长轮询会长时间占着请求级连接，连接池会被耗尽。

### [3] 账号限流

`enforce_chat_rate_limit`（`app/api/chat.py`）按 `user:<id>` 计数。
按账号而不是按 IP：反代后面只剩代理地址，NAT 用户还会互相挤占。IP 维度留给认证端点。

### [4] 模型快照

`resolve_request_model` → `resolve_chat_model`（`app/services/model_service/registry.py`）
一次读库拿到四样东西：路由键、调用目标（含 `base_url` 与密钥）、上下文窗口、白名单集合。

关键决定：**显式传入的 `model` 必须在白名单里**。`model` 只是转发给上游的字符串，
不校验就等于任何登录用户都能用服务端 key 调任意模型。库已接管但一个启用模型都没有时返回
503，而不是回退 `MODEL_NAME`——否则「停用全部模型」看起来像「没生效」。
白名单为 `None` 表示库没接管（走环境变量网关），此时显式 `model` 不校验。

出站地址在这一步校验：`outbound_guard.reject_private_target` 拒绝链路本地、未指定、
组播与云元数据地址。

### [5] reasoning_effort

`normalize_reasoning_effort` 把 `off/none/false/disabled` 归一化成 `"none"`，
再校验它是否在该模型的 `snapshot.reasoning_levels` 内（值来自数据库列的
`supported_reasoning_levels`）。模型没声明等级时放行（无法判定不等于拒绝）。
归一化必须在进 Agent 缓存之前做，否则 `"HIGH"`、`"high"`、`" high "` 各占一个缓存槽。

### 前置分支的失败形态

下面三步（[6]–[8]）的任何失败都不会变成 HTTP 状态码：

| 失败点 | 事件 | retryable | code |
|---|---|---|---|
| 目标为 `None` 且库已接管 | `error` | false | `model_unavailable` |
| 预算解析自身抛异常（如读库失败） | `error` | true | `internal_error` |
| 历史序列非法 | `error` | false | 无 |
| Agent 装配失败 | `error` | true | 无 |

前两行必须分开：`model_unavailable` 是「模型确实不存在」的确定结论，与第二行的「查不出来」
混为一谈，一次数据库抖动就会被报成「模型已下架」。

### [6] 预算与压缩

`resolve_context_budget`（`app/services/token_budget.py`）按窗口算上限：

```text
可用上限 = (窗口 − 预留 − 固定开销) × 安全系数
```

- 预留优先用上游声明的 `max_output_tokens`，否则用 `CONTEXT_RESERVE_TOKENS`（默认 8192），
  两者都会被压在窗口的一半以内。
- 固定开销 = 系统提示词 token + 工具 schema 估算（512）。
- 客户端给了 `context_anchor` 时只估算锚点那条起之后的消息（含锚点那条 assistant），但锚点会被核对：明显低于前缀真实规模
  就退回全量估算（否则填个小值就能绕过预检）。
- 窗口未知（网关没报）一律放行，不猜。

超限后按 `CONTEXT_GUARD_MODE`（默认 `compact`）处理：
`app/services/context_compaction.py` 先截断超长单条（最后一条永不截断——截了等于替用户改问题），
再折叠早期轮次成摘要；摘要超时（`SUMMARY_TIMEOUT_SECONDS = 20s`）或失败就退回确定性的抽取式摘要。
压完仍超限则发 `error`（`code=context_overflow`，`retryable=false`）。

安全系数是 `CONTEXT_SAFETY_RATIO`（默认 0.9）。固定开销只在「从零估算」这条路上扣：
走 `context_anchor` 时锚点值已是完整 prompt 的规模，再扣就扣两次
（`build_budget` 的 `used_tokens_include_overhead`）。

压缩改变了模型看到的上下文，所以只要 `strategy` 不是 `none` 就必定发一条 `compaction` 事件
（只给数字与策略，不下发摘要正文）。

### [7] 历史转换

`convert_chat_history`（`app/services/chat_history.py`）把客户端回放的历史转成 pydantic-ai
的消息序列，并校验：首条必须是 user、严格交替、普通请求以 user 结尾。续跑审批时允许以
带 `tool_calls` 的 assistant 结尾。

### [8] Agent 装配

`build_agent`（`app/services/agent_factory.py`）是**唯一**的装配点，对话、标题、摘要都走这里。
缓存键 = 归一化后的模型名 + 归一化后的 effort + settings 派生值 + 调用目标。
装配失败不是 HTTP 错误：响应头早已发出，只能发 `error`（`retryable=true`，无 code，文案带错误编号）。

装配出来的东西：主模型（配了备用模型时包一层 `FallbackModel`）、工具列表、系统提示词、
MCP toolsets、两个 capability（审批闸门 + 异常隔离）。

系统提示词走 `instructions` 而不是 `system_prompt`：服务端无状态、每轮由客户端回放历史，
而 `system_prompt` 只在历史为空时注入——第 2 轮起整份行为准则会全丢。

### [9] 流式生成

`agent.run_stream_events` 产出 pydantic-ai 事件，`app/services/event_translation.py`
把它们翻成 `app/schemas/events.py` 里的聊天事件。翻译层用 `isinstance` 分派，
未识别的类型告警一次——静默丢弃正文增量的后果是「模型没说话」，日志里没有痕迹。

#### 工具调用与作用域注入

一轮对话进入流式循环前会打开四个上下文（`app/services/chat_service.py`）：

```python
with (
    cache_scope(cache_scope_key),      # Tavily 结果缓存按用户隔离
    sandbox_scope(user_id, session_id), # 沙箱归属：服务端注入，模型不可伪造
    collect_fallbacks() as fallbacks,   # 收集容灾切换
    collect_tool_failures(),            # 统计本轮工具失败次数
):
```

作用域来自服务端注入的 `user.id` 与请求里的 `session_id`，绝不出现在工具参数里。
`session_id` 只是「最后是谁在用」的标记，不是授权凭据：租约查询一律带 `user_id`，
谎报别人的 `session_id` 也只能拿到自己名下的机器。

工具执行顺序上有两个 capability，顺序有意义：**审批闸门必须在异常隔离之前**。
反过来的话，`ApprovalRequired` 会被当成工具失败降级成文案，工具照常执行。

#### 审批中断与续跑

风险等级是 `safe_write` 或 `dangerous` 的调用被 `ApprovalGate.before_tool_execute` 拦下，
抛 `ApprovalRequired`。本轮随即以 `approval_required` 事件结束，事件里带每条调用的
`tool_call_id`、入参、工具说明与风险等级（`app/services/approval_flow.py` 从
`app/runtime/catalog.py` 取说明）。

用户决定后，客户端带同一个 `tool_call_id` 续跑。续跑时：

- `build_resume_results_for` 按历史里「有调用但没有结果」的 id 逐条取决定，
  **缺失即拒绝**（`ToolDenied`）。
- 服务端不看客户端声明，只看历史形状（`all_deferred_ids`）。
- 续跑时 `current_prompt` 是 `None` 而不是空串：空串会产出一条 content 为空的 user 消息，
  Anthropic 系直接 400。

#### 备用模型

主模型返回 5xx / 429 / 超时 / 连接失败时切换备用模型（`FallbackModel`，
判定在 `app/services/upstream_errors.py`）。401/403/400/404 不切：同一套凭据或请求本身的问题，
换模型也会失败。

切换只发生在流打开阶段（`FallbackModel` 的语义）；流中途失败交给下面的重试整轮重来。
切换成功会发一条 `fallback` 事件（`from_model` / `to_model` / `reason`）——静默切换会让能力差异
被误判成「模型变差」。

#### 失败与重试路径

| 失败点 | 处理 | 是否重试 |
|---|---|---|
| 上游 5xx / 超时 / 连接错误，**且还没向客户端产出任何事件** | 退避 0.5s 后整轮重试一次（`MAX_STREAM_ATTEMPTS = 2`） | 是 |
| 上游 5xx 等，但已经产出过事件 | 直接 `error` 事件 | 否 |
| 4xx / 429 | 不自动重试（再打一次只会加深限流） | 否 |
| 超出总时长预算 600s | `error`（`retryable=true`） | 否 |
| 工具抛异常 | 降级成失败结果回灌模型，本轮继续 | 由模型决定 |
| 客户端断开（`CancelledError`） | 原样上抛，不翻译成错误 | — |

「还没产出任何事件」是重试的前提：产出过再重试会让正文重复，有副作用的工具会跑两次。

### [10] 用量

`usage` 事件在正文之后、`done` 之前发出，用的是上游回报的真实值而不是估算。
`context_window` 取自本轮快照（备用模型接手时报备用模型的窗口）；
`context_limit_tokens` 是给前端画进度条用的 prompt 口径上限（含系统提示词与工具 schema 开销、乘过安全系数），
与后端判超限用的 `budget.limit_tokens`、以及 `compaction` 事件里的 `limit_tokens` 都不是同一个数。
量不到就不发这个事件，绝不补 0（`0 tok/s` 会被读成模型卡住）。

### [11] SSE 序列化

`app/api/chat.py` 是唯一的序列化点：`to_sse_frame` 把事件用 `model_dump_json()`
压成一行 JSON，交给 `sse-starlette` 加 `data: ` 前缀与结尾空行。
客户端按 `\r?\n\r?\n` 分帧。细节见 [API 与 SSE 契约](./api.md)。

### 客户端落库与同步

服务端不存对话历史。客户端把事件流里的正文、思考链、工具调用与结果写进 IndexedDB，
再通过 `POST /api/sync/push` 推到服务端镜像；其他设备用 `GET /api/sync/pull` 按游标增量拉。
详见 [跨设备同步](../features/sync.md)。

## 三处时间预算

| 预算 | 值 | 位置 | 语义 |
|---|---|---|---|
| 总时长 | 600s | `chat_service.TOTAL_STREAM_BUDGET_SECONDS` | 一轮对话的总体上界，含 SDK 与网关的重试 |
| 上游空闲 | 120s | `settings.MODEL_UPSTREAM_TIMEOUT` | 多久没收到数据就放弃（不是总时长，长流式不会因此被切断） |
| 单次 ASGI send | 30s | `chat.py.SSE_SEND_TIMEOUT_SECONDS` | 半开连接下 `await send` 会无限阻塞，一直占着上游额度 |

摘要另有 20s 预算（`SUMMARY_TIMEOUT_SECONDS`），标题另有 10s（`TITLE_TIMEOUT_SECONDS`）——
两者都不该跟着流式空闲超时等下去。

## 相关源码

- `app/api/chat.py` — 入口、模型解析、限流、SSE 帧
- `app/services/chat_service.py` — 一轮对话的编排、重试、容灾、预算分支
- `app/services/agent_factory.py` — Agent 装配与系统提示词渲染
- `app/services/token_budget.py` — 预算判定与锚点校验
- `app/services/context_compaction.py` — 两级压缩
- `app/services/approval_flow.py` — 审批事件与续跑结果
- `app/services/event_translation.py` — pydantic-ai 事件到聊天事件的翻译
- `app/runtime/approval.py` — 审批闸门
