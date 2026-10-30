---
title: 常见问题
description: 文档别处没直接回答的 15 个问题：会话镜像与回放、.env 里的模型为何不生效、脚本调用、沙箱生命周期、重启与传输选型。
---

# 常见问题

这页只收文档其他页面没有直接回答、但新人一定会问的问题。每个答案都能在仓库源码里对上，依据写在答案下面；已经有专门页面的问题只给链接，不在这里重复一遍。

## 会话与数据

### 服务端明明存了会话镜像，为什么对话还要客户端回放历史？

镜像不是对话的输入。`POST /api/chat` 这条链路只读请求体里的 `messages`，全程不查 `conversation` / `message` 表；两个表是同步引擎写进去的副本，不是对话的权威来源。副本还会滞后：客户端离线或 push 失败时本地比服务端新，拿镜像拼历史等于让模型看旧对话。另外审批续跑要精确的 `tool_calls` 形状，而 `message.payload` 是前端整包存的 JSONB，服务端不会据此反推消息序列。

依据：`app/api/chat.py`（只依赖用户与模型解析，不碰会话表）、`app/services/chat_service.py`、`sql/init.sql` 的 `message.payload`。无状态的取舍见[架构总览](../design/index.md)。

### 清掉浏览器数据会怎样？会话还能回来吗？

能，只要之前推上去过。同步游标存在浏览器本地（`syncMeta` 的 `cursor`），清掉之后第一次 pull 不带游标，服务端按 `since=0` 全量返回会话与消息行，一页拉不完就按 `has_more` 继续翻。代价是从头拉一遍，受单次 500 行 / 8 MB 的响应上限约束。清掉之前没推上去的内容没有第二份，本地优先不等于服务端也有。

依据：`app/services/sync_service.py:parse_cursor`（无游标即 0）与 `pull()`、`frontend/src/lib/sync/engine.ts:175`、`app/config/settings.py` 的 `sync_pull_max_rows` / `sync_pull_max_bytes`。

### 有没有会话与消息数量上限？

有，都是容量保护而不是付费分级：单用户最多 2000 条未删除会话，单会话最多 5000 条消息。超限时 push 的对应行返回 `quota_exceeded`，同批其余行照常接受；客户端把它标成 error，等用户清理旧内容后手动重新上传。两者都改自 `sync_max_conversations_per_user` / `sync_max_messages_per_conversation`。

依据：`app/config/settings.py:715-725`、`app/services/sync_service.py:388-422, 532-533`。拒绝原因表见[跨设备同步](../features/sync.md)。

### 登录能同时在多台设备吗？登出会踢掉别的设备吗？

能。每次登录写一条新的 `accesstoken` 行（cookie 里是原文，库里只存 SHA-256 摘要），各设备互不干扰，有效期统一按 `SESSION_LIFETIME_SECONDS`（默认 30 天）算。登出只删当前这条会话行，其余设备不受影响。要一次性踢掉全部设备用改密码、封禁或改角色，它们会删该用户的全部会话行。

依据：`app/services/auth.py:HashedDatabaseStrategy.write_token`、`app/db/maintenance.py:delete_user_access_tokens`、`app/db/models/user.py`。

## 模型与上游

### 我在 `.env` 里改了 `MODEL_NAME`，为什么用户看不到新模型？

环境变量只在「从未初始化过」的库上引导一次：启动时若已有注册表标记或任一提供商行，`seed_default_provider()` 直接返回 False，不写库。之后模型解析只读数据库，改 `.env` 再重启既不会新增模型，也不会改默认模型。要加模型去管理后台手动添加或点「同步模型」。

依据：`app/services/model_service/provider_sync.py:seed_default_provider`、`app/services/model_service/provider_repo.py:registry_configured`、`app/server.py` 的 lifespan。

### 为什么模型清单放在数据库而不是配置文件里？

因为它是运营数据，运行期就要改：启停、设默认、从上游 `/v1/models` 同步上下文窗口与输出上限，这些都随上游和运营需求变化，而配置在进程启动时只读一次，改配置就得重启一次进程。放库里换来的是「改完立即生效」——代价是列表不做任何缓存，每次解析模型都要读一次库（一次查询拿回整份快照）。

依据：`app/services/model_service/registry.py` 模块说明与 `resolve_chat_model`、`app/api/admin.py`、[配置手册](configuration.md)的模型网关一节。

### 评测和调优打的是线上那个模型吗？

不是。离线评测（`prompt-lab`）用独立的一组变量 `QUALITY_BASE_URL` / `QUALITY_API_KEY` / `QUALITY_MODEL`，默认打本机 OpenAI 兼容代理的 `group/deepseek-v4.1-flash`；它既不读 `LITELLM_*`，也不读数据库里的模型清单，所以调它不会占用平台里给用户配的模型与密钥，也不会随后台改模型而漂移。只有 `--target live` 打真实 `/api/chat`，那时才用库里启用的模型与线上额度。

依据：`quality/targets/drivers.py:22-25`、`quality/targets/live.py`。两个目标的差别见[质量平台](../design/quality-platform.md)。

### 能不能用脚本或其他程序调 `/api/chat`？有没有 API Key？

没有 API Key 机制：认证只有 HttpOnly cookie 会话（`accesstoken` 表），仓库里没有签发访问令牌的接口，`/api/chat` 也不读 `Authorization`。脚本要先用**表单编码**调 `POST /api/auth/login`（字段 `username` 填邮箱、`password`），拿到 `bennett_session` cookie 后带上它发请求。

依据：`app/services/auth.py:auth_backend`（cookie 传输 + 数据库策略）、`app/api/auth.py`、端点清单见 [API 与 SSE 契约](../design/api.md)。

### 接本机 Ollama 或 LM Studio 要填什么？

后台新建提供商时选内置预设即可：Ollama 是 `http://localhost:11434/v1`、`auth_style=none`、不需要 key；LM Studio 是 `http://localhost:1234/v1`、同样免鉴权。两个预设的说明都写着一件事：`PROVIDER_BLOCK_PRIVATE_TARGETS=true` 之后创建会被拒，因为环回地址在这一档被拦。另外 `localhost` 指后端进程自己所在的网络命名空间，后端跑在容器里时它不是宿主机。

依据：`app/services/model_service/presets.py:145-164`、`app/config/settings.py:provider_block_private_targets`。

## 沙箱与工具

### 沙箱是一个会话一台还是一个用户一台？多开标签页会怎样？

按用户，跨会话共用一台：活动状态下 `user_id` 上有部分唯一索引，`session_id` 只做归因、不参与寻址。多开标签页拿到的是同一台机器——心跳键是租约级的 `sbx:hb:<lease_id>`，任一可见标签页都在给它续命，被切到后台的标签页不发心跳。所以在一个标签页里点重置（`sandbox_reset`）会连同另一个标签页装好的依赖与文件一起清掉。

依据：`sql/init.sql` 的 `ux_sandbox_lease_one_per_user`、`app/services/sandbox/lease.py:heartbeat_key`、`frontend/src/hooks/use-sandbox-heartbeat.ts`。完整生命周期见[沙箱执行](../features/sandbox.md)。

### 沙箱里的文件怎么拿到本地？

只有销毁前的产物导出这一条路，而且只在**回收循环**里发生：空闲回收或硬 TTL 到点时把 workspace 打包成 `workspace.tar.gz`（超过 `SANDBOX_ARTIFACT_MAX_BYTES`，默认 200 MB，就只导 `manifest.txt` 文件清单）传到对象存储，再由 `GET /api/sandbox/artifacts?session_id=...` 换成预签名直链下载。用户主动调 `sandbox_reset` 走的是另一条路径：直接销毁，不导出产物。运行中的机器没有文件下载接口；没配对象存储时这个端点返回空列表而不是报错。

依据：`app/services/sandbox/reaper.py:171`、`app/tools/sandbox.py:sandbox_reset`、`app/api/sandbox.py:sandbox_artifacts`、`app/services/sandbox/artifacts.py`、`app/config/settings.py:sandbox_artifact_max_bytes`。

### 关掉页面或点停止，正在跑的工具会怎样？

客户端中止请求时会 cancel 响应体，服务端把这个断开当成 `CancelledError` 原样上抛，本轮就此结束，不会再有 `done` 事件——前端把这种收尾标成「回复可能不完整」。已经发出去的沙箱命令不会因为连接断开而中止，它要到自己的 `timeout_s` 才结束；机器也不会立刻回收，心跳虽然停了，但要等空闲窗口（默认 600 秒，从最后一次活动或心跳算起）才拆。

依据：`app/services/chat_service.py:163, 524`、`frontend/src/lib/chat-transport.ts:parseEventStream`、`app/services/sandbox/lease_service.py:touch`、`app/config/settings.py:sandbox_idle_seconds`。

## 部署与运行

### 能不能不接对象存储？

能。对象存储只服务沙箱产物导出，不配时导出静默跳过，对话、同步、模型都不受影响。开了沙箱却没配，`/health/ready` 的 `storage` 项会报 `unavailable / not_configured`，整体降级成 `degraded`（仍返回 200，除非你把它写进 `READINESS_REQUIRED_DEPENDENCIES`）；不开沙箱时这一项显式报 `disabled`，不会假装正常。

依据：`app/services/storage/__init__.py` 模块说明、`app/api/ops.py:_storage_health`。探针口径见[可观测性](../ops/observability.md)。

### 重启后端会掐断正在进行的对话吗？

会。优雅关闭只等 5 秒（`timeout_graceful_shutdown=5`），远小于单轮 600 秒的预算，在飞的 SSE 会被中止，前端按不完整收尾。历史不受影响——浏览器 IndexedDB 与服务端镜像都还在，重连后下一轮用客户端回放的历史继续。停机顺序也是刻意的：先等 `serve()` 返回再置回收循环的 stop，正在销毁的沙箱租约不会停在 `destroying` 没人管。

依据：`main.py:_run_all`、`frontend/src/lib/chat-transport.ts`。一轮对话的时间预算见[一轮对话的链路](../design/request-lifecycle.md)。

### 为什么用 SSE，不用 WebSocket 或 EventSource？

一轮对话就是一个带请求体的 POST，服务端不持有连接状态；需要「服务端等用户」的两处（审批、澄清）都做成「本轮结束 + 客户端续跑」，单向流已经够用。浏览器原生 `EventSource` 只发 GET、不能带请求体、也不能在流中读非 200 的响应体，所以前端自己用 fetch + ReadableStream 解析，顺带拿到 AbortController 与 60 秒空闲看门狗。

依据：`app/api/chat.py:chat_stream`、`app/services/approval_flow.py` 模块说明、`frontend/src/lib/chat-transport.ts`。

## 仍然没答案时

- 有具体症状（起不来、一直 503、流卡住、同步不动）：先查[故障排查](../ops/troubleshooting.md)的「症状 → 先看哪里」，每条都给了验证命令与日志关键字。
- 想知道一轮请求内部发生了什么：[一轮对话的链路](../design/request-lifecycle.md)按步骤标了判定点与失败路径。
- 对端点、状态码、SSE 字段有疑问：[API 与 SSE 契约](../design/api.md)是接口面的权威清单。
- 想知道某个功能默认开不开：[功能总览](../features/index.md)的能力矩阵，判据是 `GET /api/capabilities` 的运行时结果。
- 本页没覆盖的问题，多数能在对应页末尾的「相关源码」里找到落点；改代码前先看[测试体系](../ops/testing.md)怎么跑。
