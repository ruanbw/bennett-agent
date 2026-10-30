---
title: 对话与流式事件
description: 一轮对话怎么跑：服务端无状态、SSE 分帧、全部事件类型、审批续跑与三段超时边界。
---

`POST /api/chat` 是唯一的对话入口。它返回一条 SSE 流，流里是类型化事件；服务端不留会话状态，历史每轮由客户端全量回放。

## 服务端无状态，客户端持有历史

请求体（`app/schemas/chat.py:ChatRequest`）带完整消息列表：

```json
{
  "model": "provider/gpt-4o",
  "reasoning_effort": "high",
  "messages": [
    {"role": "user", "content": "帮我跑一下这个脚本"},
    {"role": "assistant", "content": "好的",
     "tool_calls": [{"tool_call_id": "call_1", "tool_name": "sandbox_exec", "args": "{\"command\":\"ls\"}"}]}
  ],
  "context_anchor": {"message_index": 1, "context_tokens": 4210},
  "session_id": "sess_abc123",
  "approvals": {"call_1": true}
}
```

服务端对历史有几条硬约束，它们同时是防注入的边界：

- `role` 只允许 `user` / `assistant`。`system` 会被当成系统提示词注入，所以直接拒绝。
- 必须以 `user` 开头、`user`/`assistant` 严格交替、正常请求以 `user` 结尾。续跑审批时允许以带 `tool_calls` 的 `assistant` 结尾。
- `tool_calls` 只能挂在 `assistant` 消息上，且只接受 `tool_call_id` / `tool_name` / `args` 三个字段——**不接受结果**。回放结果等于允许伪造「删除已成功」。
- 单条消息 ≤ 32000 字符、单条 thinking ≤ 64000 字符、消息条数 ≤ 100、历史总量 ≤ 400000 字符、单个工具入参 ≤ 64000 字符。
- 非文本内容分片直接拒绝，而不是静默丢弃：用户以为图片传上去了、模型只看到一句文字，比报错更糟。

所有校验失败都是 422，请求体不进对话流。

## SSE 分帧

服务端在 `app/api/chat.py:to_sse_frame` 一处把事件序列化：

```text
data: {"type":"content","delta":"你"}

data: {"type":"done"}
```

`model_dump_json()` 的紧凑分隔符会把裸换行转义成 `\n`，所以一帧永远是一行 JSON，帧之间是一个空行。客户端按 `/\r?\n\r?\n/` 分帧、按 `\r?\n` 分行、剥掉 `data:` 前缀。

响应头有两处特殊处理：

| 头 | 原因 |
|---|---|
| `X-Accel-Buffering: no` | 否则反向代理会把流式输出攒成一整块，用户看不到逐字输出 |
| 删掉 `Connection: keep-alive` | sse-starlette 在合并响应头后仍无条件写入该头，HTTP/2 禁止它，网关会重置流 |

另外两个传输层约束：

- **单次 send 超时 30 秒**。半开连接下 `await send` 会无限阻塞，一直占着上游额度。
- **前端 60 秒空闲看门狗**。连续 60 秒没收到任何字节就中止请求并按「回复可能不完整」收尾。sse-starlette 默认每 15 秒发一条 `: ping` 注释帧，活着的连接不会真的静默这么久。

## 全部事件类型

判别键是 `type`，契约在 `app/schemas/events.py`。下表按前端要做的动作排列：

| `type` | 关键字段 | 前端动作 |
|---|---|---|
| `thinking` | `delta` | 追加到思考块；第一次收到时记思考起点 |
| `content` | `delta` | 追加到正文；正文与思考是两块，不混排 |
| `tool_call` | `tool_call_id`、`tool_name`、`args` | 插入一条「运行中」的工具记录；同 id 不重复插入 |
| `tool_result` | `tool_call_id`、`result`、`outcome` | 把同 id 记录改成完成态；`outcome` 决定显示成功还是失败 |
| `clarification` | `tool_call_id`、`question`、`options`、`allow_custom_input` | 弹出选项卡片；允许时带自定义输入框 |
| `approval_required` | `tool_calls[]`（含 `args`、`description`、`risk_level`） | 渲染审批卡片，**本轮到此结束**，等用户决定后续跑 |
| `fallback` | `from_model`、`to_model`、`reason` | 显示一行「已切换到备用模型」；不拼进正文 |
| `compaction` | `strategy`、`dropped_messages`、`truncated_messages`、`used_tokens_before/after`、`limit_tokens` | 显示一行压缩提示（含省了多少） |
| `usage` | `context_tokens`、`context_window`、`context_limit_tokens`、`output_tokens`、`cache_read_tokens`、`timing` | 更新上下文预算进度条与计时药丸 |
| `error` | `message`、`retryable`、`code` | 显示错误；`retryable` 决定是否给「重试」按钮 |
| `done` | — | 标记本轮正常结束 |

`outcome` 有四态：`success`、`failed`、`denied`、`interrupted`。前端不能把隔离后的错误文案当成一次成功调用。

事件流必以 `done` 或 `error` 收尾。

### 三个字段的口径陷阱

- `context_tokens` 是**最后一次请求**的 prompt token，不是本轮累加。
- `context_limit_tokens` 才是后端判超限时用的上限，与 `context_window` 不是一回事。
- `cache_read_tokens` 是 `context_tokens` 的子集，不是额外开销。

计时（`usage.timing`）七个字段是同一个块：没量到就整块缺席，前端绝不补 0——`0 tok/s` 会被读成模型卡住。`tool_ms` 无工具时是真实的 0，`ttft_ms` / `decode_ms` / `decode_tokens` 只在真实解码窗口里成对出现。

## 审批：只能分两段

SSE 是单向的，服务端无状态，所以「等用户点批准」不能靠阻塞。pydantic-ai 的机制是：需要批准的调用不执行，本轮输出变成 `DeferredToolRequests`，服务端把它翻成 `approval_required` 事件（`app/services/approval_flow.py`），**本轮到此结束**。

客户端要做的：

1. 把 `tool_calls` 原样持久化（含 `tool_call_id` 与 `args`）。
2. 用户逐条决定。
3. 续跑时把决定放进 `approvals`（`tool_call_id` → 布尔），并把带 `tool_calls` 的那条 assistant 消息原样回放。

服务端怎么认这是续跑：**看历史形状**——`approvals` 非空且历史最后一条是 assistant。只看 `approvals` 不够：客户端可能顺手带上旧审批记录，那种应按普通请求处理。

两条硬规则：

- **缺失的决定一律按拒绝处理。** 关掉页面不能等于同意删除。
- 拒绝也要显式回灌 `ToolDenied`，并附一句「是用户拒绝、不是工具坏了」——否则模型会重试同一个调用（再弹一次审批框），或者编一个「已完成」的回答。

审批续跑是分段续跑：批准后服务端只执行那批被拦下的调用，模型接着往下走。

## 澄清提问

澄清也是一个工具（`ask_user_clarification`），但事件层把它翻译成独立的 `clarification` 事件，客户端不必知道这个工具名。

校验规则只有一份实现（`app/runtime/clarification.py:normalize_clarification`），工具层与事件翻译层共用——规则写两处会出现「工具报错但弹窗已经弹了」，模型带新 `tool_call_id` 重试会绕过去重，用户看到两个弹窗。

规则：问题非空且 ≤ 500 字符，选项 2~10 个、每个 ≤ 100 字符，去重保序。`allow_custom_input` 默认允许。

一轮里同一个 `tool_call_id` 只弹一次（`seen_clarification_ids` 去重）。意图确认工具的结果事件不下发——它是 agent 循环的内部细节。

## 错误码与可重试性

`error` 事件的 `code` 只有三个取值，`retryable` 只决定客户端是否显示重试按钮：

| `code` | 含义 | 可重试 |
|---|---|---|
| `context_overflow` | 上下文超限，重试必败 | false |
| `model_unavailable` | 模型已停用或不存在 | false |
| `internal_error` | 服务端自身出错（附错误编号） | true |

`code` 为空（`null`）的旧服务端路径，`retryable` 由上游状态码决定：400/401/403/404 为 false（同一个请求再发结果不变），其余为 true。

内部异常原文只进日志，回给用户的只有一个 12 位错误编号（`_new_error_id()`）——异常原文可能含上游 URL、provider 上下文乃至密钥。

上游错误文案按状态码映射：400/404 →「模型不可用，请更换模型」，401/403 →「上游认证配置问题，请联系管理员」，429 →「上游限流，请稍后重试」，5xx 与超时 →「服务暂时不可用，请重试」。

## 备用模型切换

主模型返回 5xx / 429 / 超时 / 连接失败时自动切备用模型，并下发 `fallback` 事件。401/403/400/404 不切：同一套凭据或请求本身的问题，换模型也救不回来。

切换只发生在流打开阶段（中途失败不接管）。事件带 `reason` 类别短语（「上游限流」「上游 503」「请求超时」「连接失败」），上游错误原文只进日志。

静默切换会让能力差异被误判成模型变差，所以这件事必须可见。

## 标题生成

`POST /api/chat/title` 单独一个端点：把首条提示词（≤ 500 字符，`MAX_TITLE_INPUT_CHARS`）交给一个不挂工具的专用 agent，产出 ≤ 20 字的标题。

- 标题是装饰：上游超时只有 10 秒（`TITLE_TIMEOUT_SECONDS`），远小于流式的 120 秒空闲超时。
- 失败一律退回首行截断（15 字），再不行就是「新对话」，不冒泡给用户。
- 前端是 fire-and-forget：失败保留占位标题。

## 三段超时边界

| 边界 | 值 | 位置 | 语义 |
|---|---|---|---|
| 单轮总时长 | 600 秒 | `chat_service.TOTAL_STREAM_BUDGET_SECONDS` | 本轮对话的总体上界。超时中止并提示「生成超时，请重试」 |
| 上游空闲超时 | 120 秒 | `MODEL_UPSTREAM_TIMEOUT` | 多久没收到**数据**就放弃。不是总时长，长流式输出不会被切断 |
| 单次 send 超时 | 30 秒 | `chat.py:SSE_SEND_TIMEOUT_SECONDS` | 半开连接的兜底，防止 `await send` 无限阻塞占着上游额度 |

600 秒这一道是必要的外层上界：上游超时只管「多久没收到数据」，叠加 SDK 与网关的重试后，单轮仍可能拖到十几分钟。

重试规则：只在**还没向客户端产出任何事件**时才重试（最多 2 次，退避 0.5 秒），只针对 5xx / 超时 / 连接类错误。产出过事件再重试会让正文重复、有副作用的工具跑两次。

## 相关源码

- `app/api/chat.py` — SSE 端点、模型白名单校验、思考等级校验、事件序列化
- `app/schemas/chat.py` — 请求体契约与全部输入边界
- `app/schemas/events.py` — 事件类型定义（判别键 `type`）
- `app/services/chat_service.py` — 预算、压缩、历史转换、重试、事件产出的编排
- `app/services/event_translation.py` — pydantic-ai 流事件 → `ChatEvent`
- `app/services/approval_flow.py` — 审批事件构造与续跑结果
- `app/services/chat_history.py` — 客户端历史 → pydantic-ai 消息序列
- `app/services/upstream_errors.py` — 上游错误分类与用户文案
