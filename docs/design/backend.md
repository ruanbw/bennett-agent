---
title: 后端分层与装配
description: 各层职责与禁止边界、抽出 runtime 的理由、启动与停机顺序、--reload 的实现。
---

# 后端分层与装配

后端是一棵自上而下的依赖树：接口层依赖编排层，编排层依赖执行面与数据层，
执行面不依赖任何上层。本页说明每层放什么、为什么这么切，以及进程启动与停机时发生了什么。

## 各层职责

### `app/api/` — 接口层

只做四件事：路由、SSE 分帧、把服务层异常映射成 HTTP 状态码、声明认证与限流依赖。

- 不写业务逻辑。`app/api/chat.py` 里没有一行「怎么算预算」「怎么切备用模型」。
- 不直接碰数据库（管理后台的列表与角色 UPDATE、沙箱三端点查租约例外：就是一层薄查询）。
- SSE 的序列化只在 `app/api/chat.py` 一处发生：事件形状定义在 `app/schemas/events.py`，
  但「怎么变成 `data: ...`」只有这一行代码。

### `app/services/` — 编排层

一轮对话的完整流程在这里，但每个模块只负责一件事：

| 模块 | 职责 |
|---|---|
| `chat_service.py` | 一轮对话的编排：预算 → 压缩 → 装配 → 流式 → 重试 → 容灾 → 事件产出 |
| `agent_factory.py` | 唯一的 Agent 装配点；对话 / 标题 / 摘要共用它 |
| `context_compaction.py` | 两级压缩（截断超长单条 → 折叠早期轮次） |
| `token_budget.py` | token 估算与预算快照 |
| `chat_history.py` | 客户端回放历史 → pydantic-ai 消息序列 |
| `event_translation.py` | pydantic-ai 流事件 → 聊天事件 |
| `approval_flow.py` | 待审批请求 → 前端事件；客户端决定 → 续跑结果 |
| `auth.py` | cookie 会话后端与 `current_active_user` / `current_admin` 依赖（唯一 import FastAPI 的服务模块）|
| `model_service/*` | 提供商与模型的读写、上游同步、模型解析、出站守卫 |
| `sandbox/*` | 租约状态机、provider 适配、回收循环、产物导出 |
| `sync_service.py` | 服务端权威版本号、push/pull |
| `storage/*` | 对象存储：沙箱产物导出与产物直链（`base.py` / `s3.py`） |
| `http_client.py` / `redis_client.py` | 进程级共享连接池 |

### `app/runtime/` — 工具执行面

这一层是「工具怎么被管住」的规则所在，与工具实现无关：

- `approval.py` — 风险等级到执行闸门，含续跑时的悬空调用判定
- `guard.py` — 把工具的意外异常降级成失败结果
- `scopes.py` — 缓存作用域与沙箱作用域的注入
- `tool_registry.py` — `ManagedTool`、`RiskLevel`、`ToolCategory` 与注册表
- `catalog.py` — 按名字查工具说明与风险等级（审批框要的那两行字）
- `clarification.py` — 澄清提问入参校验

### `app/tools/` — 装配层

工具实现与注册表：联网搜索、四个沙箱工具、澄清提问、当前时间、MCP 客户端。
`build_tools()` 按当前配置产出可挂载给 Agent 的列表。

工具签名里刻意没有 id 类参数：沙箱工具只有 `command`、`path` 这类，
归属靠 `app/runtime/scopes.py` 注入。

### `app/db/` — 数据层

ORM 模型、异步引擎、会话热缓存、结构守卫。表结构的唯一事实来源是 `sql/init.sql`：
启动时执行一次（幂等），再逐列比对模型与库，漂移就拒绝启动并给出可复制的 `ALTER TABLE`。
`DATABASE_APPLY_SCHEMA=false` 时这两步都跳过（只记一条 warning），迁移交给外部工具时用。

`app/db/__init__.py` 是唯一对外面：`from app.db import User` 到处都能用，
调用方不需要知道名字在哪个子模块。

### `app/config/` — 配置层

`settings.py`（全部配置项与启动期校验）、`secret_box.py`（API Key 落库加密）、
`logging_setup.py`（统一日志格式）、`proxy_env.py`（`no_proxy` 规范化）。

### `app/prompts/` — 提示词

Jinja2 模板加载与渲染。`prompts/system_prompt.jinja` 按实际装配的工具列表渲染：
没挂载 Tavily 就不会教模型去搜索，没挂载沙箱就不会教模型写脚本。
提示词里出现的工具名永远来自真实装配结果，不写死。

## 为什么抽出 runtime

没有 runtime 时，审批闸门、沙箱归属、工具异常隔离这三种「规则」只能长在 `app/tools` 里，
而 `app/tools` 依赖 `app.services.sandbox`（租约）与 `app.services.http_client`/`redis_client`，
于是 `app/services` 的编排模块一旦为了拿规则而 import `app.tools`，就形成包级环。
现在服务层的编排模块（`chat_service`、`approval_flow`、`event_translation`）只 import
`app.runtime`，不 import `app.tools`。

拆出 `app/runtime/` 之后：

```text
services ──依赖──▶ runtime ◀──依赖── tools
```

runtime 只定义规则与最小接口（`ToolCatalog` 就是一个 `describe(name)` 的 Protocol），
不 import 工具实现，也不 import 数据库。装配时把实现注入（`set_tool_builder_for_tests`
换掉工具装配口、`set_catalog_for_tests` 换掉 catalog）。

收益不只是「没有环」：

1. **审批闸门可以按元数据生效**，而不是按工具名字硬编码。工具是动态装配的，MCP 的风险等级
   运行时才知道。
2. **规则可单测**。`requires_approval`、`all_deferred_ids`、`normalize_clarification`
   都是纯函数或接近纯函数，不需要起 FastAPI、不需要连库。
3. **旧路径仍可用**。`app/tools/approval.py`、`guard.py`、`catalog.py`、`registry.py`、
   `sandbox_scope.py`、`cache_scope.py` 六个垫片模块都还在，只做 re-export：老调用点与测试
   按旧路径 import 不会断，新代码一律写 `app.runtime`。

## 工具惰性装配

`app/tools/__init__.py` 在 import 期不注册任何工具。`get_registry()` 首次调用才把实现拉起来。

理由：服务层与测试只要碰到 `app.tools` 的任一子模块，就顺带把沙箱与 Tavily 的实现栈
（httpx 客户端、OpenSandbox SDK、租约逻辑）装进内存。注册表本身用模块级 `_REGISTRY` 缓存，
`reset_registry()` 可丢弃重建。

同一原则在别处复用：

- `agent_factory._default_tool_builder` 在函数内 `from app.tools import build_tools`——
  装配 agent 是 services 的职责，不该在 import 期把整个工具包连带拉起。
- `runtime/catalog.py` 查注册表也用函数内 import。
- `agent_factory._tool_capabilities()` 惰性构造 `ApprovalGate` 与 `ToolErrorIsolation`。

`build_tools()` 每次调用都重新按配置判断（`tavily` 的 key 可能两轮之间才配上），
所以调用方必须把结果纳入缓存键——`agent_factory._settings_cache_key()` 里有
`is_usable_api_key(settings.tavily_api_key)`（只取布尔值，避免密钥原文进键）。

## 启动顺序

`app/server.py` 的 `lifespan` 按固定顺序执行，失败的处理各不相同：

| 步骤 | 失败后果 | 位置 |
|---|---|---|
| 1. 提示词自检（编译全部模板 + 渲染一次系统提示词） | **中止启动** | `validate_prompts()` |
| 2. 建表（执行 `sql/init.sql` + 逐列比对） | **中止启动** | `verify_schema_at_head()` |
| 3. 提供商引导（库里没有提供商时用环境变量建一条默认网关） | 告警，继续 | `seed_default_provider()` |
| 4. 清理过期会话行 | 告警，继续 | `_cleanup_expired_sessions_on_startup()` |
| 5. 认证限流形态告警（反代部署但没配代理网段时） | 告警，继续 | `_warn_if_auth_rate_limit_is_global()` |
| 6. 提供商 API Key 解密自检（逐条试解，点名解不开的） | 告警，继续 | `_warn_if_provider_keys_are_undecryptable()` |
| 7. 对象存储（开沙箱时确保桶存在 + 可达性检查） | 告警，继续 | `_ensure_object_storage()` |
| 8. MCP 工具发现（让提示词与能力探针拿到真实清单） | 告警，继续 | `_discover_mcp_tools()` |

前两步能中止启动，是因为它们失败后服务一定不能正常干活：模板坏了会在第一条用户消息的
预算预检里炸成「请求处理失败」；表没建好则认证依赖查 `user` 表直接 500。
后面的步骤都是增强能力，失败只该降级不该拦住服务。

提示词自检会真渲染一次而不只是编译：语法通过不代表能渲染，缺变量（`StrictUndefined`）
与「渲染成空串」只有真渲染才暴露。

MCP 发现放在启动而不是第一轮：发现要建连，放到第一轮会撞上 SSE 的总预算；
失败不阻止启动（内置工具照常可用），但连不上的 server 会记进 `discovery_error`，
并出现在 `/api/capabilities` 的 `mcp_error` 字段里——只让工具从列表里消失，
用户会以为配置从来没生效。

## API 与回收共用一个事件循环

`main.py` 的 `_run_all` 用 `asyncio.run` 起一个事件循环，同时跑两件事：

```text
asyncio.run(_run_all(...))
  ├─ asyncio.create_task(run_reaper(stop))   # 沙箱空闲回收，间隔 60s
  └─ await server.serve()                     # uvicorn
```

必须共用一个 loop：数据库引擎、限流器、Redis 客户端都是进程级单例，
连接绑在创建它的 loop 上，起子进程就是两份连接池。

### 停机顺序

```text
serve() 返回
  ▼
stop.set()                      # 先置位，让回收循环知道该退
  ▼
await asyncio.wait(loops, 10)   # 给回收循环 10s 收尾
  ▼
task.cancel()
  ▼
close_redis() → close_http_client() → engine.dispose()
```

`stop.set()` 必须在 `serve()` 返回之后：反过来的话，正在销毁的沙箱会停在 `destroying` 状态，
没有任何代码能回收它。三个关闭动作各自 try/except，互不遮蔽。

uvicorn 侧关掉了两样东西，都是有意的：

- `proxy_headers=False`——默认开启会让本机连接凭 `X-Forwarded-For` 改写 `client.host`，
  绕开应用层的限流键信任配置。
- `timeout_graceful_shutdown=5`——在飞 SSE 最长 600s，等所有任务结束会让 Ctrl-C 像卡死。
  不设宽限意味着在飞请求会被中断，这是可接受的代价（生产建议走反代平滑重启）。

## `--reload` 的实现与生产禁用

`--reload` 不用 `uvicorn --reload`，而是自己实现（`app/dev_reload.py`）：
父进程用 `watchfiles` 监听 `app/`、`prompts/`、`main.py` 与实际加载的 `.env*`，
检测到变更就 SIGTERM 掉子进程再起一个新的 `python main.py`。

自己实现的原因：`uvicorn --reload` 用 spawn 只 import `app.server:app`，
会丢掉 `main.py` 里的日志格式与 `no_proxy` 清理，启动路径与部署不一致。
模板与 `.env` 不进 import 缓存，只能靠重启。

细节：

- 防抖 500ms——编辑器原子保存是「临时文件 + rename」，不防抖会连着重启两次。
- 排除 `*.db` / `-wal` / `-shm`：当成变更源就变成「一有请求就重启」。
- 只认 `settings.get_env_files()` 列出的 `.env` 文件，不认 `.env.example`。
- 等子进程退出 6s（大于 uvicorn 的 5s 宽限），超时才 SIGKILL。

生产禁用是硬拒绝：`main.py` 在 `APP_ENV=production` 时直接 `parser.error` 退出。
热重启会在文件变化时反复重启，每次都掐断在飞的请求（预算 600s，优雅关闭只等 5s）。
生产改配置请走重启或重建镜像。

## 相关源码

- `main.py` — 入口、事件循环、停机顺序、`--reload` 分流
- `app/server.py` — FastAPI 装配、中间件顺序、lifespan
- `app/dev_reload.py` — 开发期热重启
- `app/runtime/tool_registry.py` — `ManagedTool` 与风险等级
- `app/tools/__init__.py` — 惰性注册表与 `build_tools()`
- `app/services/agent_factory.py` — 装配缓存键与工具装配口
- `app/prompts/manager.py` — 模板加载、编译、渲染
