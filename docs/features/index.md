---
title: 功能总览
description: Bennett Agent 的能力矩阵：每项功能默认是否开启、依赖什么、在哪页展开。
---

Bennett Agent 是一套自托管的多租户 Agent 平台。核心只有四件事：**模型接入**、**工具调用**、**隔离执行**、**上下文预算**。其余能力（认证、跨设备同步、管理后台）围绕这四件事提供支撑。

这一页给一张能力矩阵和几条贯穿全局的设计约定；每项功能的具体契约在各自的页面。

## 能力矩阵

| 功能 | 默认状态 | 依赖 | 详情 |
|---|---|---|---|
| 多提供商模型接入 | 开启 | 管理后台里至少启用一个模型；或用 `.env` 引导出的默认网关 | [模型接入与容灾](./models.md) |
| 对话流式输出（SSE） | 开启 | 上游网关可达 | [对话与流式事件](./chat.md) |
| 备用模型容灾 | 关闭（未配 `FALLBACK_MODEL_NAME` 时无兜底） | 备用模型必须与主模型同提供商且已启用 | [模型接入与容灾](./models.md) |
| 联网搜索（Tavily） | 关闭（未配 Key 就不装配） | `TAVILY_API_KEY` 非占位值 | [工具系统](./tools.md) |
| 澄清提问 | 开启 | 无 | [工具系统](./tools.md) |
| 当前时间 | 开启 | 无 | [工具系统](./tools.md) |
| MCP 外部工具 | 关闭（`MCP_SERVERS` 为空） | `MCP_SERVERS` 声明 + server 可达 | [工具系统](./tools.md) |
| 沙箱代码执行 | 关闭（`SANDBOX_ENABLED=false`） | OpenSandbox 服务 + 对象存储（导出产物） | [沙箱执行](./sandbox.md) |
| 上下文预算与压缩 | 开启（`CONTEXT_GUARD_MODE=compact`） | 网关上报 `context_window` | [上下文预算与压缩](./context.md) |
| 工具审批（HITL） | 开启 | 无 | [工具系统](./tools.md) |

「默认状态」指全新部署、只用 `.env.example` 起步时的状态。真正的判据永远是运行时装配结果：`GET /api/capabilities` 返回当前实际挂载的工具清单。

## 贯穿全局的四个约定

**服务端无状态。** 会话历史由客户端持有，每轮请求全量回放。对话链路本身不存状态，因此不需要 sticky session，扩缩容只是加进程；但长轮询的唤醒表只在进程内存里，多 worker 部署时会静默退化成 25 秒超时（见 [跨设备同步](./sync.md)）。代价是每轮都要重发历史，字符上限（40 万）挡不住 token 溢出——那是 [上下文预算](./context.md) 的事。

**工具是受管能力，不是裸函数。** 每个工具带分类、风险等级、标签、启用检查器与超时；写入类操作执行前必须拿到用户批准；任何工具抛出的意外异常都被隔离成一条失败结果，不会让整轮回答作废。详见 [工具系统](./tools.md)。

**外部返回按不可信数据处理。** `tavily_search` 与 MCP 工具的结果都被加上定界符，明确标记为「资料」而非「指令」；摘要同理（它由用户内容派生）。系统提示词里另有一条通用规则，把工具返回的内容一律当数据而不是指令。

**权限靠服务端注入，不靠模型参数。** 沙箱租约按 `user_id` 解析（`session_id` 只做归因），工具缓存按用户 id 隔离。模型手里没有可伪造的句柄，越权在构造上不可达。

## 一轮对话的核心链路

```text
浏览器
  │  POST /api/chat  { model, reasoning_effort, messages[], approvals{}, context_anchor?, session_id? }
  ▼
app/api/chat.py         解析模型（白名单 + 窗口上限）、校验思考等级、按账号限流
  ▼
app/services/chat_service.py
  │    ├─ resolve_context_budget   估 token，超限则压缩（context_compaction.py）
  │    ├─ convert_chat_history     客户端历史 → pydantic-ai 消息序列
  │    ├─ build_agent               装配模型 / 工具 / 审批闸门 / 异常隔离
  │    └─ agent.run_stream_events   事件流 → ChatEvent（event_translation.py）
  ▼
SSE 帧（每帧一个 ChatEvent 的 JSON）
  ▼
浏览器（chat-store.ts 折叠成 run 状态）
```

服务端只产出类型化事件，序列化只发生在 `app/api/chat.py` 一处。事件类型与前端动作见 [对话与流式事件](./chat.md)。

## 未配置可选能力时的降级

三项可选能力都遵循同一条原则：**能力缺失时降级成「明确告诉模型做不到」，而不是静默失败或假装成功**。

| 缺失项 | 表现 | 代码依据 |
|---|---|---|
| 未配 `TAVILY_API_KEY`（或仍是 `tvly-your-api-key` 占位） | 搜索工具根本不装配，模型看不到它；系统提示词里对应段落改写成「本轮没有联网搜索工具，不要声称自己查询了网络」 | `is_usable_api_key()`、`app/tools/__init__.py` |
| 未配 `MCP_SERVERS` | 不装配任何 toolset，工具列表里没有 MCP 工具，提示词不列外部工具段落 | `app/services/agent_factory.py:_mcp_toolsets` |
| MCP 声明写错（格式、非法 server 名、重复名） | **启动失败**，不静默跳过——声明出来的能力不能悄悄消失 | `load_server_configs()` 抛 `ValueError` |
| MCP server 运行时连不上 | 该 server 的工具本轮缺席，原因记进 `discovery_error`，经 `/api/capabilities` 的 `mcp_error` 暴露；其余 server 与内置工具照常 | `McpRegistry.discover()` |
| `SANDBOX_ENABLED=false` | 四个沙箱工具不装配，模型看不到；系统提示词改写成「本轮没有隔离执行环境：无法运行代码或读写文件……不要假装跑过」 | `is_sandbox_enabled()`、`prompts/system_prompt.jinja` |
| 沙箱服务不可达 | 工具返回一句中文失败文案（「执行环境暂时不可用，请稍后再试」），对话其余部分照常 | `app/tools/sandbox.py:_error_message` |
| 网关没上报 `context_window` | 该模型一律放行，不做上下文判定；只按模型 id 告警一次 | `resolve_context_budget()` |
| 没有任何可用模型 | `POST /api/chat` 返回 503「管理员尚未启用任何模型」 | `resolve_request_model()` |

::: warning
「工具不装配」和「工具装配了但会失败」是两回事。前者模型根本看不到这个工具，系统提示词也会同步改写；后者模型会调用，然后拿到一句失败文案。前者更诚实——所以未配置的 Tavily 与沙箱一律选择不装配。
:::

## 相关源码

- `app/api/chat.py` — SSE 端点、模型解析、限流、事件序列化
- `app/services/chat_service.py` — 一轮对话的编排（预算、压缩、重试、事件产出）
- `app/schemas/events.py` — 全部 SSE 事件的类型化契约
- `app/tools/__init__.py` — 工具注册表的装配入口与内置工具元数据
- `app/runtime/tool_registry.py` — 分类、风险等级、标签、超时、启用检查
- `app/services/token_budget.py` — 上下文预算的估算与上限计算
- `app/services/sandbox/lease.py` — 沙箱租约状态机、心跳、命令审计
- `app/config/settings.py` — 全部配置项与默认值
