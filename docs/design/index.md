---
title: 架构总览
description: Bennett Agent 的分层结构、包边界、六条关键设计决定与进程级单例清单。
---

# 架构总览

Bennett Agent 是一个多租户 AI 对话平台：两套 React 前端（用户端、管理后台）共享一个 FastAPI 后端，
后端把「对话编排」「工具执行」「沙箱」「同步」四件事放在同一个进程里，但用包边界隔开。

本页只回答三件事：系统长什么样、为什么这样切、哪些东西是进程级单例。单个组件的细节去
[一轮对话的链路](./request-lifecycle.md) 与 [后端分层与装配](./backend.md)。

## 分层图

```text
┌────────────────────┐   ┌────────────────────┐
│  用户端 React 19   │   │  管理后台 React 19 │
│  frontend/ :5173   │   │ admin-frontend/    │
│ IndexedDB 本地优先 │   │      :5174         │
└─────────┬──────────┘   └─────────┬──────────┘
          │  Cookie 会话              │  Cookie 会话
          │  /api/*                   │  /api/admin/*
          └────────────┬──────────────┘
                       ▼
┌──────────────────────────────────────────────────────────────┐
│ FastAPI（app/server.py）                                     │
│  中间件 6 层（由外到内，顺序见下文）                        │
│  路由：auth / chat / capabilities / sandbox / sync / admin / ops│
└───────────────┬──────────────────────────────────────────────┘
                ▼
┌──────────────────────────────────────────────────────────────┐
│ services（编排层）                                            │
│  chat_service 编排、token_budget 预算、context_compaction 压缩、 │
│  agent_factory 唯一的 Agent 装配点、model_service/* 模型解析、   │
│  sandbox/* 租约与回收、sync_service 同步、title_service 标题    │
└───────┬──────────────────────────────────┬───────────────────┘
        ▼                                  ▼
┌───────────────────────┐   ┌──────────────────────────────┐
│ runtime（工具执行面）  │   │ tools（装配层）                │
│  approval 审批闸门      │   │  registry 编目、sandbox 四个、  │
│  guard 异常隔离        │   │  tavily_search、ask_user、    │
│  scopes 作用域注入      │   │  current_time、mcp_client     │
│  tool_registry 元数据  │   │                              │
│  catalog 说明查询      │   └──────────────────────────────┘
└───────┬───────────────┘
        ▼
┌──────────────────────────────────────────────────────────────┐
│ 外部依赖                                                       │
│  PostgreSQL（权威数据） / Redis（限流·缓存·心跳）              │
│  S3 协议对象存储（沙箱产物） / OpenSandbox（隔离执行环境）      │
│  上游模型网关（OpenAI 兼容 + LiteLLM 路由）                     │
└──────────────────────────────────────────────────────────────┘
```

前端不进后端进程：`app/server.py` 只挂 `/api/*`，不挂前端构建产物。前端是独立的静态产物，
经 `VITE_API_BASE_URL` 直连后端（同源反代时相对路径即可）。

图里 services 只列了主线模块；还有 `storage/*`（对象存储，只服务沙箱产物导出与产物直链）和
`title_service`（会话标题，失败时回退到首行截断）。

中间件在 `create_app()` 里按「请求体上限 → Origin 守卫 → CORS → 指标 → 请求 ID → 安全头」注册，
而 Starlette 把**最后注册的放在最外层**，所以实际执行顺序是倒序：安全头 → 请求 ID → 指标 →
CORS → Origin 守卫 → 请求体上限。注册顺序是刻意的：请求体上限与 Origin 守卫早于 CORS 注册，
413/403 才会带上 CORS 头（`app/server.py` 的注释就写在注册处）。

## 包边界与依赖方向

依赖只允许自上而下，同层之间靠接口而不是互相 import：

| 层 | 包 | 职责 | 不允许知道 |
|---|---|---|---|
| 接口层 | `app/api/` | HTTP 路由、SSE 分帧、限流与认证依赖 | 工具实现细节、预算与容灾规则 |
| 编排层 | `app/services/` | 一轮对话的编排、预算、压缩、重试、容灾 | Request/Response（`auth.py` 例外，它实现的是 FastAPI 依赖）|
| 执行面 | `app/runtime/` | 工具风险闸门、异常隔离、作用域、注册表 | 数据库、上游模型 |
| 装配层 | `app/tools/` | 工具实现与注册表，产出可挂载的 Tool 列表 | Agent 装配细节 |
| 数据层 | `app/db/` | ORM 模型、引擎、会话缓存、结构守卫 | 业务用例 |
| 配置层 | `app/config/` | Settings、密钥加密、日志、代理环境 | 一切上层 |
| 提示词 | `app/prompts/` | Jinja2 模板加载与渲染 | 工具与模型 |

两条硬约束：

1. **`app/runtime/` 是 services 与 tools 之间的解耦层**。审批闸门、异常隔离、作用域注入这三类
   「规则」都放在 runtime，两边都只从 runtime 取规则。注意这并不等于「互不依赖」：
   `app/tools/` 仍然 import `app.services.sandbox`（租约）与 `app.services.http_client` /
   `redis_client`（共享连接池），而 services 一侧只在
   `agent_factory._default_tool_builder()` 的函数体内 import `app.tools`——规则单向、装配走端口。
   详见 [后端分层与装配](./backend.md#为什么抽出-runtime)。
2. **`app/api/` 是唯一的序列化点**。`ChatEvent` 只在 `app/api/chat.py` 一处变成 SSE 帧；
   `app/schemas/` 是请求与响应的形状定义，`app/schemas/events.py` 用 `type` 作判别键。

`app/tools/` 下的 `approval.py`、`guard.py`、`catalog.py`、`registry.py`、`sandbox_scope.py`、
`cache_scope.py` 六个模块都还在，只做 re-export，实现已经搬到 `app/runtime/`：老调用点与测试
按旧路径 import 仍然可用，新代码一律写 `app.runtime`。

## 六条关键设计决定

### 1. 服务无状态，历史由客户端持有

`POST /api/chat` 每次都携带完整历史（`messages`），服务端不留会话状态。带来的后果与代价：

- 后端可以水平扩容，不依赖 sticky session；进程崩了，正在进行的对话最多丢当前这一轮。
- 上下文预算必须由服务端每次重新估算，因为服务端没有「上一轮说了什么」的记录。
  客户端可以回传上一轮的真实用量锚点（`context_anchor`）换取精度，但服务端仍然会校验它。
- 历史合法性必须在入口拦截：`app/schemas/chat.py` 校验交替顺序与首尾角色，
  `app/services/chat_history.py` 再校验一遍。

### 2. 同步用服务端权威版本号

会话与消息的镜像存在服务端（`conversation` / `message` 表），每行带一个单调递增的
`version`。版本号由服务端发号（`sync_counter` 表，`INSERT ... ON CONFLICT DO UPDATE ... RETURNING`），
与业务写入同事务，避免「版本前进、行还没写」的空洞。

客户端的 `client_rev` 只用于拒绝更旧的重放，不参与排序也不当游标。拉取用不透明游标
（`v1:<version>`，前缀定义在 `app/schemas/sync.py`），服务端可以随时换编码。两种坏游标分开返回：
格式不认识回 `400`（`code=invalid_cursor`）；游标指向当前数据之外（服务端回滚 / 旧库恢复）回
`410`（`code=resync_required`）。对客户端动作相同：丢掉游标重新全量拉。

### 3. 工具受管，执行前有闸门

工具不是裸函数。每个工具在 `app/runtime/tool_registry.py` 里带分类、风险等级、超时、
启用开关。风险等级的分界是**能不能撤销**，不是有多危险：`read_only` 与 `interactive` 直接执行，
`safe_write` 与 `dangerous` 执行前必须拿到用户批准（`app/runtime/approval.py`）。

审批在无状态 SSE 上分两段实现：本轮抛 `ApprovalRequired` 变成 `DeferredToolRequests`，
客户端把待审批的调用原样存下来，用户点完之后带同一个 `tool_call_id` 续跑。
**缺失的决定一律按拒绝处理**：关掉页面不等于同意删除。

工具抛出的意外异常不会作废整轮，由 `app/runtime/guard.py` 降级成一条失败结果回灌给模型。

### 4. 沙箱按用户隔离

每个用户一台隔离执行环境（`sandbox_lease`），同一用户跨会话共用——别的会话装过的依赖、
写过的文件都在里面。工具签名里没有任何 id：服务端用登录用户与会话标识构造作用域
（`app/runtime/scopes.py`），租约寻址只看 `user_id`；模型手里没有可伪造的句柄。

provider 层还有一道 `owner_tag`（`sha256(user_id)[:16]`）核对：库里那行租约和实际那台机器
对不上时，操作会被拒而不是作用到别人的机器上。

### 5. 预算前置，不等上游报错

每轮生成前先估算 token 用量并判定是否超限（`app/services/token_budget.py`）。超限时按
`CONTEXT_GUARD_MODE` 处理，默认 `compact`：先截断超长单条，再折叠早期轮次成摘要，
压不下去才拒绝。压缩会改模型看到的上下文，所以只要真压了（`strategy` 不是 `none`）就发一条 `compaction` 事件告诉用户。

### 6. 质量平台与生产共用一份 Trace

`quality/` 不另造一套可观测性。评测、调优、安全扫描读的 Trace 契约（`quality/models.py`）
与生产 SSE 事件（`app/schemas/events.py`）是同一条事件流的两种投影：
`live` 目标直接打真实 `/api/chat`，把 SSE 事件翻成 Trace。详见
[质量平台](./quality-platform.md)。

## 进程级单例

这些东西在一个进程里只有一份，原因各不相同：

| 单例 | 位置 | 为什么必须是单例 |
|---|---|---|
| 数据库引擎 | `app/db/base.py` | 连接池绑定创建它的事件循环，换 loop 复用会抛 `Future attached to a different loop` |
| Redis 客户端 | `app/services/redis_client.py` | 共享连接池；每请求建连会让 TLS 握手与连接数成为瓶颈 |
| httpx 客户端 | `app/services/http_client.py` | 同上；池上限 20，防止上游挂掉时句柄堆积 |
| 工具注册表 | `app/tools/__init__.py` | 惰性构造：import 期注册会把整条实现栈拉进每个进程 |
| MCP 注册表 | `app/tools/mcp_client.py` | toolset 与发现结果都缓存到进程内 |
| Agent 装配缓存 | `app/services/agent_factory.py` | `lru_cache(32)`，键含归一化参数与 settings 派生值 |
| 提示词管理器 | `app/prompts/manager.py` | Jinja2 环境与模板缓存 |
| 限流器 | `app/api/rate_limit.py` | 配额变化时重建，不是常驻常量 |

::: warning
「单例」不等于「进程间共享」。限流计数默认走 Redis（跨实例共享配额），Redis 不可用时降级为
进程内计数——此时配额等于乘以实例数，降级会告警并计入 `redis_unavailable_total`。
同理，`app/services/sync_notifier.py` 的长轮询唤醒表只在进程内存里，多 worker 部署必须换成
Redis pub/sub，否则跨进程写入唤不醒等待方。
:::

## 相关源码

- `main.py` — 运行入口：API 与沙箱回收共用一个事件循环
- `app/server.py` — FastAPI 装配、中间件顺序、lifespan
- `app/middleware.py` — 请求体上限、跨站守卫、安全头
- `app/observability.py` — 请求关联 ID 与 HTTP 指标
- `app/runtime/approval.py` — 审批闸门与续跑结果
- `app/runtime/scopes.py` — 缓存与沙箱作用域注入
- `app/services/agent_factory.py` — 唯一的 Agent 装配点
- `app/services/sync_service.py` — 服务端权威版本号与增量同步
