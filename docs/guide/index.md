---
title: 文档导览
description: 按角色给出阅读路径、文档地图与仓库目录职责，说明哪些部分不在本仓库。
---

# 文档导览

Bennett Agent 是一个自托管的多租户 AI 对话平台：后端是 FastAPI + PydanticAI + LiteLLM 的 SSE 流式服务，前端是 React 19 + Vite 的两个独立应用（用户端、管理后台），数据落在 PostgreSQL / Redis / S3 兼容对象存储，代码执行交给 OpenSandbox 沙箱。本组「指南」覆盖把它跑起来、改动它、部署它的全过程。

文档按**你要做的事**组织，不是按代码模块组织。每页只讲一个主题，页末列出相关源码路径。

## 按角色的阅读路径

| 你的目标 | 建议顺序 | 读完后能做什么 |
|---|---|---|
| 第一次在本机跑起来 | [环境准备](prerequisites.md) → [本地运行](quickstart.md) | 后端、用户端、管理后台三个进程都在本机起来并能登录对话 |
| 部署到服务器 | [环境准备](prerequisites.md) → [配置手册](configuration.md) → [生产部署](deployment.md) | 用 `app` profile 起完整服务，前端静态托管，配置反代与备份 |
| 改后端代码 | [配置手册](configuration.md) → [本地运行](quickstart.md) → [测试体系](../ops/testing.md) | 知道配置从哪读、启动期有哪些守卫、怎么跑测试 |
| 改前端代码 | [前端开发与构建](frontend.md) → [本地运行](quickstart.md) | 知道两个应用的分工、代理规则、构建产物落到哪里 |
| 评估质量与放行 | [配置手册](configuration.md)（模型网关一节） → [测试体系](../ops/testing.md) → [质量平台](../design/quality-platform.md) | 知道评测跑在哪个模型上、密钥从哪来，以及评测/调优/安全扫描怎么跑 |

只想先把项目转起来看效果，读前两页就够；要上线，请完整读「环境准备 → 配置手册 → 生产部署」。

## 文档地图

本组（指南）：

| 页面 | 解决的问题 |
|---|---|
| [环境准备](prerequisites.md) | 需要装什么、每个端口的出处、每项怎么验证 |
| [本地运行](quickstart.md) | 从空目录到能登录对话的完整命令序列 |
| [配置手册](configuration.md) | 每个配置组的作用、边界与启动期守卫 |
| [前端开发与构建](frontend.md) | 两个 React 应用怎么分工、怎么构建、怎么托管 |
| [生产部署](deployment.md) | compose 编排、反向代理、沙箱、备份与上线清单 |

站点的其他分组（在站点侧栏「功能 / 设计 / 运维 / 参考」下）：

| 分组 | 内容 |
|---|---|
| 功能 | 每个用户可见能力怎么用：对话、工具、沙箱、上下文预算、模型接入、账号与 RBAC、同步、管理后台 |
| 设计 | 一轮对话的链路、后端分层、数据模型、API 与 SSE 契约、安全设计 |
| 运维 | 测试体系、可观测性、故障排查 |

## 仓库目录职责

```text
bennett-agent/
├── main.py             # 运行入口：API + 沙箱回收共用一个事件循环；--reload 仅开发
├── app/                # 后端源码
│   ├── api/            # HTTP 路由层（auth / chat / capabilities / admin / sync / sandbox / ops）
│   ├── services/       # 业务层（agent 装配、上下文预算、模型注册表、沙箱、同步）
│   ├── runtime/        # 工具执行面：审批闸门、异常隔离、作用域、注册表
│   ├── tools/          # 工具目录与受管能力（Tavily、sandbox、MCP、澄清）
│   ├── schemas/        # 请求 / 响应与 SSE 事件的形状定义
│   ├── config/         # 配置加载、日志、代理环境清理、密钥加密
│   ├── db/             # SQLAlchemy 模型与引擎（engine 在 base.py）
│   └── server.py       # FastAPI 装配：中间件顺序、路由挂载、启动自检
├── sql/init.sql        # 数据库结构的唯一事实来源（整段幂等）
├── prompts/            # Jinja2 系统提示词模板
├── frontend/           # 用户端（React 19 + Vite，dev 端口 5173）
├── admin-frontend/     # 管理后台（React 19 + Vite + react-router，dev 端口 5174）
├── evals/              # 行为回归评测用例（cases.yaml）与框架
├── quality/            # 评测 / 安全扫描 / 调优 / 门禁闭环（python -m quality）
├── scripts/            # seed_admin、run_eval、smoke_sandbox 等运维脚本
├── tests/              # pytest 测试（零基础设施）
├── docker/             # 沙箱镜像与 OpenSandbox 本地配置
├── docker-compose.yml  # 开发依赖 + 生产编排（app profile）
├── Dockerfile          # 后端镜像（不含前端产物）
├── docs/               # VitePress 文档站（本页所在）与 public/ 下的演示物料
└── .github/workflows/  # CI：后端 / 评测框架 / 两个前端检查，以及文档站构建发布
```

两个前端是**独立工程**，各自有 `package.json`、依赖锁与 Vite 配置；后端镜像里没有前端产物，构建与托管是独立一步。

## 不在本仓库的部分

| 部分 | 在哪里 | 说明 |
|---|---|---|
| 前端静态托管 | 你自己的静态服务器或 CDN | `npm run build` 的产物交给任意静态服务，本仓库不提供托管进程 |
| 反向代理 | Nginx / Caddy / 云网关 | `/api` 转发、SSE 不缓冲、可信代理头都由代理侧配置 |
| 模型网关 | 外部 LiteLLM Proxy 或 OpenAI 兼容端点 | 本仓库只存地址与密钥，不含网关实现 |
| 对象存储 | 外部 S3 兼容服务（AWS S3 / 阿里云 OSS / 自建 RustFS） | 本仓库只有 compose 里的本地 RustFS，生产可换 |
| 沙箱节点池 | OpenSandbox server（本地 Docker 运行时 / 生产 K8s + Firecracker） | 后端只当客户端，节点池由另一套部署负责 |
| 密钥管理 | 你的 KMS / Secret Manager | 生产 `AUTH_SECRET` 与各 API Key 应由密钥系统注入，而不是写进 `.env` |

这些边界是刻意的：把「谁负责托管」「谁负责网关」写清楚，部署时才知道该在哪一层解决问题。

## 相关源码

- `main.py`
- `app/server.py`
- `app/config/settings.py`
- `docker-compose.yml`
- `Dockerfile`
- `frontend/vite.config.js`
- `admin-frontend/vite.config.js`
