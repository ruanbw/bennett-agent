---
layout: home
title: Bennett Agent
titleTemplate: false
description: 自托管的多租户 AI 对话助手：环境准备、本地运行、功能说明、设计取舍与运维排障。

hero:
  name: Bennett Agent
  text: 自托管的多租户 AI 对话助手
  tagline: PydanticAI + LiteLLM 的 Agent 框架，FastAPI SSE 流式后端，React 19 本地优先前端，配独立管理后台与每用户沙箱。
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
    details: 统一走 OpenAI 兼容端点 + LiteLLM 路由格式，内置主流厂商预设与 Ollama / LM Studio 本地模型；API Key 加密落库，管理后台可同步、启停、设默认。
  - title: 完整的 Agent 工具面
    details: 联网搜索、沙箱代码执行、澄清提问，以及通过 MCP 动态发现的外部工具。工具统一编目分类、风险等级与超时，危险操作走审批闸门。
  - title: 每用户隔离沙箱
    details: 每个用户一台持久执行环境，心跳续命、状态机回收、命令审计；本地 Docker 运行时，生产可切 K8s + Firecracker。
  - title: 上下文预算
    details: 每轮按网关 context_window 估算用量，超限两级压缩（截断超长消息 → 折叠旧轮次为摘要），压缩对用户可见。
  - title: 模型容灾与可观测
    details: 主模型 5xx / 429 / 超时自动切备用；SSE 事件带用量、计时与错误分类；Prometheus 指标、就绪探针、结构化日志。
  - title: 本地优先 + 跨设备同步
    details: 会话存 IndexedDB，断网不丢已同步内容；push 行级幂等 upsert、pull 不透明游标长轮询，服务端权威版本号。
---

## 这套系统解决什么问题

Bennett Agent 不是「调用一次模型返回一段文本」的演示，而是一套可以自己部署、可以多租户运营的对话平台。它把 Agent 应用里最容易出错、也最值得工程化的部分显式做出来：

- **状态在哪**：会话历史由客户端持有，服务端无状态；跨设备靠增量同步，不靠 sticky session。
- **模型怎么换**：提供商与模型清单是数据库里的运营数据，不是写死在配置里的常量；失败时按可恢复性分类切备用模型。
- **工具怎么放权**：工具不是裸函数，而是带分类、风险等级、超时、审批和异常隔离的受管能力。
- **上下文怎么不炸**：预算判定、两级压缩、压缩可见，而不是等上游返回 400 再报「请求失败」。
- **成本与质量怎么量化**：`quality/` 对真实提示词与工具契约做评测、调优、安全扫描和发布门禁。

## 文档地图

| 你想做的事 | 从这里开始 |
|---|---|
| 把项目在本地跑起来 | [环境准备](/guide/prerequisites) → [本地运行](/guide/quickstart) |
| 了解每个功能怎么用 | [功能总览](/features/) |
| 看一轮对话在系统里怎么流动 | [一轮对话的链路](/design/request-lifecycle) |
| 部署到服务器 | [生产部署](/guide/deployment) |
| 改代码前先知道测试怎么跑 | [测试体系](/ops/testing) |
| 看真实运行效果 | [在线演示](https://ruanbw.github.io/bennett-agent/demo/) |

## 技术栈

| 层 | 技术 |
|---|---|
| 后端 | Python 3.10+、FastAPI、PydanticAI、LiteLLM、SQLAlchemy (asyncpg)、uv |
| 前端 | React 19、Vite、TypeScript、Tailwind CSS 4、zustand、Dexie (IndexedDB) |
| 数据层 | PostgreSQL 17、Redis 7（限流 / 缓存 / 会话热缓存）、S3 协议对象存储 |
| 沙箱 | OpenSandbox（本地 Docker 运行时 / 生产 K8s + Firecracker） |
| 文档 | VitePress，GitHub Pages 自动部署 |
