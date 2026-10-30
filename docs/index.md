---
layout: home
title: Bennett Agent
titleTemplate: false
description: 自托管的多租户 AI 对话平台：模型接入、工具审批、沙箱执行、上下文预算、跨设备同步与运维边界。

hero:
  name: Bennett Agent
  text: 自托管的多租户 AI 对话平台
  tagline: PydanticAI 装配 Agent 与工具，LiteLLM 路由键接入任意 OpenAI 兼容模型；FastAPI + SSE 流式后端，React 19 本地优先前端，独立管理后台，每用户一台沙箱。
  actions:
    - theme: brand
      text: 快速开始
      link: /guide/quickstart
    - theme: alt
      text: 功能总览
      link: /features/
    - theme: alt
      text: 在线演示
      link: /demo/
      target: _blank

features:
  - title: 多提供商模型接入
    details: 所有厂商统一走 OpenAI 兼容端点，对外是 LiteLLM 路由键（提供商 slug + 模型 id）；内置厂商预设与本机 Ollama / LM Studio。API Key 加密落库，管理后台负责同步模型列表、启停与设默认。
  - title: 完整的 Agent 工具面
    details: 联网搜索、沙箱代码执行、澄清提问、当前时间，以及通过 MCP 动态发现的外部工具。每个工具带分类、风险等级与超时（澄清与取当前时间两个工具不设硬超时）；写入与危险操作执行前必须拿到用户批准。
  - title: 每用户隔离沙箱
    details: 一个用户跨所有会话共用一台持久执行环境，靠部分唯一索引兜底；心跳在 Redis、状态机在数据库、命令逐条审计。本地用 Docker 运行时，生产切 K8s + Firecracker。
  - title: 上下文预算
    details: 每轮以网关上报的 context_window 为上限估算用量（网关没报就放行），超限两级压缩：截断超长消息的中间段 → 折叠早期轮次为摘要；压缩会作为 compaction 事件推给前端，带策略与前后用量。
  - title: 模型容灾与可观测
    details: 主模型 5xx / 429 / 超时切备用模型（须同提供商且已启用）；SSE 事件带用量、计时与错误分类。另有存活与就绪探针、Prometheus 文本格式指标（默认关闭）、带请求 ID 的日志。
  - title: 本地优先 + 跨设备同步
    details: 会话存 IndexedDB，断网不丢已同步内容；push 是行级幂等 upsert，pull 是不透明游标长轮询，版本号由服务端 sync_counter 发号。
---

## 这套系统解决什么问题

Bennett Agent 不是「调用一次模型返回一段文本」的演示，而是一套能自己部署、能多租户运营的对话平台。下面五件事在 Agent 应用里最容易出错：

- **状态在哪**：每轮请求自带完整历史，`ChatRequest`（`app/schemas/chat.py`）校验形状（首条 user、user/assistant 交替、末条 user）与总量（100 条 / 40 万字符）。服务端进程不保存对话状态，也就没有 sticky session；跨设备一致性靠同步接口，`version` 由每个用户一行 `sync_counter` 用 `INSERT ... ON CONFLICT DO UPDATE` 发号，客户端只拿到不透明游标。
- **模型怎么换**：提供商与模型是数据库里的运营数据（`model_provider` / `provider_model`），后台改完立即生效，不重启进程。失败按可恢复性分类（`app/services/upstream_errors.py`）：5xx、超时、连接错误先同模型重试一次；429 与上述错误切备用模型；401/403/400/404 不切——同一套凭据或同一个坏请求，换模型救不回来。
- **工具怎么放权**：工具带分类、风险等级与超时（`app/runtime/tool_registry.py`），`safe_write` 与 `dangerous` 执行前必须拿到用户批准。审批不阻塞 SSE：首轮抛 `ApprovalRequired`，客户端带同一个 `tool_call_id` 续跑（`app/runtime/approval.py`）。工具抛出的意外异常被降级成一条失败结果（`app/runtime/guard.py`），不会让整轮回答作废。
- **上下文怎么不炸**：`app/services/token_budget.py` 按字符类别估 token（CJK 1 字 1 token，其余 4 字 1 token），可用上一轮真实用量做锚点校准。超限时 `app/services/context_compaction.py` 先截断超长消息的中间段，再把早期轮次折叠成摘要，摘要调用失败退回确定性的抽取式摘要；压缩通过 `compaction` 事件告知前端策略与前后用量，而不是等上游返回 400 再报「请求失败」。
- **成本与质量怎么量化**：`python -m quality` 有 `run`（评测）、`scan`（安全扫描）、`tune`（调优）、`gate`（与基线比，回归即非 0）四条命令；`evals/cases.yaml` 是行为回归用例。

## 不在范围内

这些部分刻意不做进本仓库。完整边界见[文档导览](/guide/#不在本仓库的部分)：

- **模型网关**：本仓库只存提供商的 Base URL 与密钥，按 OpenAI 兼容协议直连上游，不含网关进程。需要统一配额或多模型聚合时，把 `LITELLM_API_BASE` 指向外部的 LiteLLM Proxy，代码不用改。
- **前端静态托管**：`npm run build` 的产物交给任意静态服务器或 CDN；本仓库不提供托管进程，也不含反向代理配置（`/api` 转发、SSE 不缓冲都在代理侧）。
- **沙箱节点池**：后端只是 OpenSandbox 的客户端。节点池（本地 Docker 运行时 / 生产 K8s + Firecracker）由另一套部署负责，本仓库只有开发用的本地配置。

## 文档地图

| 你想做的事 | 从这里开始 |
|---|---|
| 把项目在本地跑起来 | [环境准备](/guide/prerequisites) → [本地运行](/guide/quickstart) |
| 了解每个功能怎么用 | [功能总览](/features/) |
| 看一轮对话在系统里怎么流动 | [一轮对话的链路](/design/request-lifecycle) |
| 部署到服务器 | [生产部署](/guide/deployment) |
| 上线后看指标、查故障 | [可观测性](/ops/observability) → [故障排查](/ops/troubleshooting) |
| 改代码前先知道测试怎么跑 | [测试体系](/ops/testing) |
| 看真实运行效果 | [在线演示](https://ruanbw.github.io/bennett-agent/demo/) |

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.10+、FastAPI、PydanticAI、LiteLLM、SQLAlchemy (asyncpg)、uv |
| 前端 | React 19、Vite、TypeScript、Tailwind CSS 4、zustand、Dexie (IndexedDB) |
| 数据层 | PostgreSQL 17、Redis 7（限流计数 / 业务缓存 / 会话热缓存）、S3 协议对象存储 |
| 沙箱 | OpenSandbox（本地 Docker 运行时 / 生产 K8s + Firecracker） |
| 文档 | VitePress，GitHub Pages 自动部署 |
