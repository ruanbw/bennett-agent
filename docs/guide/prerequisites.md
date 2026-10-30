---
title: 环境准备
description: 列出运行 Bennett Agent 需要的操作系统、工具链、端口与服务，并给出逐项验证命令。
---

# 环境准备

这页列全跑起一套 Bennett Agent 需要装什么、占哪些端口、每一项怎么自己验证。所有命令都可以直接复制执行。

## 工具链

| 组件 | 版本要求 | 用途 | 验证命令 |
|---|---|---|---|
| 操作系统 | Linux / macOS / Windows（WSL2） | 跑后端与 Docker | `uname -a` |
| Python | >= 3.10（仓库用 `.python-version` 锁 3.10） | 后端运行时 | `python3 --version` |
| uv | 任意近期版本 | 依赖安装与虚拟环境 | `uv --version` |
| Node.js | >= 20.11 | 两个前端的构建与 dev server | `node --version` |
| Docker + Compose v2 | Docker Engine 24+ | 起 Postgres / Redis / RustFS，生产跑 app | `docker compose version` |
| OpenSandbox | 可选 | 沙箱代码执行 | `uvx opensandbox-server --help` |

最低组合是「Python + uv + Node + Docker」：Docker 只负责三个依赖服务，后端与前端都在宿主机上直接跑。

### 安装参考

- uv：`curl -LsSf https://astral.sh/uv/install.sh | sh`，然后 `source ~/.bashrc`（macOS 用 zsh 的改 `~/.zshrc`）。
- Node：官方安装包或 nvm；版本低于 20.11 时 Vite 8 会直接拒绝启动。
- Docker Compose v2 是 `docker compose`（没有连字符）；`docker-compose` 是 v1，本仓库的 `profiles` 语法依赖 v2。

## 外部依赖（可选，但对话要用）

| 依赖 | 作用 | 说明 |
|---|---|---|
| 模型网关 | 提供模型列表与推理 | 任何 OpenAI 兼容端点 + LiteLLM 路由格式的模型名，例如 `openai/gpt-4o` |
| Tavily API Key | 联网搜索工具 | 留空时搜索工具不会被装配 |
| PostgreSQL 17 | 业务库 | compose 已提供 |
| Redis 7 | 限流计数、缓存 | compose 已提供 |
| S3 兼容存储 | 上传原件与沙箱产物 | compose 里的 RustFS，生产可换 AWS S3 / 阿里云 OSS |

模型网关的地址与密钥通过 `.env` 里的 `LITELLM_API_BASE` / `LITELLM_API_KEY` 提供。首次启动会把它引导成一条「默认网关」提供商记录，之后由管理后台维护——详见[配置手册](configuration.md)。

## 端口占用

| 端口 | 归属 | 谁在用 | 备注 |
|---|---|---|---|
| 8000 | 后端 | `uv run python main.py` / compose 的 `app` 服务 | 默认只听 `127.0.0.1` |
| 5173 | 用户端 | `cd frontend && npm run dev` | Vite dev server |
| 5174 | 管理后台 | `cd admin-frontend && npm run dev` | 与 5173 并存是常态 |
| 5432 | PostgreSQL | compose `postgres` | 只发布到回环 |
| 6379 | Redis | compose `redis` | 只发布到回环，默认无密码 |
| 9000 | 对象存储 S3 API | compose `rustfs` | 只发布到回环 |
| 9001 | RustFS 控制台 | compose `rustfs` | 建桶、看对象 |
| 8080 | OpenSandbox server | `uvx opensandbox-server` | 仅启用沙箱时需要 |

后端与两个前端 dev server 都默认绑回环。compose 里的 Postgres / Redis / RustFS 同样只绑 `127.0.0.1`——去掉这个前缀等于把库和缓存开放给同网段。

## 逐项验证

```bash
# Python 版本（必须 >= 3.10）
python3 --version

# uv
uv --version

# Node（必须 >= 20.11）
node --version && npm --version

# Docker 与 Compose v2
docker version --format '{{.Server.Version}}'
docker compose version

# 端口是否已被占用（8000 / 5173 / 5174）
lsof -nP -iTCP:8000 -sTCP:LISTEN
lsof -nP -iTCP:5173 -sTCP:LISTEN
lsof -nP -iTCP:5174 -sTCP:LISTEN
```

依赖服务起好之后：

```bash
# PostgreSQL
docker compose exec postgres pg_isready -U bennett -d bennett

# Redis
docker compose exec redis redis-cli ping        # 期望 PONG

# 对象存储（S3 API 健康端点）
curl -fsS http://127.0.0.1:9000/health
```

## 代理环境：no_proxy 必须清理

Docker Desktop（以及部分 CI 运行器）会把宿主的 `HTTP_PROXY` / `HTTPS_PROXY` 注入每个容器。若 `no_proxy` 里没有内部服务名，容器内的 `http://rustfs:9000` 会被送进宿主代理——代理不认识 `rustfs`，返回 502，而应用侧看到的报错是「确保桶存在失败（code=502）」，与「对象存储没起来」几乎一样难查。

`main.py` 在启动时调用 `app/config/proxy_env.py:sanitize_no_proxy_env()` 清理坏掉的条目（方括号 IPv6 会被 httpx 拼成非法 URL，导致所有 `trust_env` 的 HTTP 客户端在构造阶段就失败）。但**正确的做法仍是自己在 shell 里配好**：

```bash
export no_proxy="localhost,127.0.0.1,host.docker.internal,postgres,redis,rustfs"
export NO_PROXY="$no_proxy"
```

容器内这几个名字由 compose 的 `environment:` 段自动追加（`postgres` / `redis` / `rustfs`），宿主机直跑时只需要 `localhost,127.0.0.1`。

## `.env` 最小必填项

从 `.env.example` 复制一份再改：

```bash
cp .env.example .env
```

最小集合（生产环境的强制项见[生产部署](deployment.md)）：

| 变量 | 必填 | 说明 |
|---|---|---|
| `AUTH_SECRET` | 是 | 会话令牌签名密钥，兼做数据库里 API Key 的加密密钥。生产必须是 >= 32 字符的强随机串 |
| `LITELLM_API_BASE` | 是 | 模型网关地址，例如 `http://127.0.0.1:4000` |
| `LITELLM_API_KEY` | 是 | 网关密钥 |
| `MODEL_NAME` | 是 | 默认模型，LiteLLM 路由格式 |
| `DATABASE_URL` | 否 | 默认指向 compose 里的 Postgres，通常不用改 |
| `REDIS_URL` | 否 | 默认指向 compose 里的 Redis |
| `S3_ENDPOINT_URL` / `S3_ACCESS_KEY` / `S3_SECRET_KEY` | 否 | 沙箱产物导出需要；关掉沙箱可留空 |
| `TAVILY_API_KEY` | 否 | 留空则不装配搜索工具 |

生成 `AUTH_SECRET`：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

::: warning
不要把真实密钥写进仓库里的任何文件。`.gitignore` 已忽略 `.env*`，但已经提交过的密钥需要轮换。
:::

## 启用沙箱需要额外准备

沙箱默认关闭。启用需要三样东西：沙箱镜像、OpenSandbox server、后端开关。

```bash
# 1. 构建沙箱镜像（版本号要与 SANDBOX_TEMPLATE_VERSION 对应）
docker build -f docker/sandbox.Dockerfile -t bennett-sandbox:0.1 docker/

# 2. 起 OpenSandbox server（本地 Docker 运行时）
OPENSANDBOX_INSECURE_SERVER=YES uvx opensandbox-server --config docker/opensandbox.toml

# 3. .env 里改开关
# SANDBOX_ENABLED=true
# SANDBOX_SERVER_URL=http://127.0.0.1:8080
```

`OPENSANDBOX_INSECURE_SERVER=YES` 是显式确认「本机开发、无鉴权」。生产必须给 server 配 API Key，并且只在内网暴露。步骤细节见[本地运行](quickstart.md)。

## 相关源码

- `pyproject.toml`
- `.python-version`
- `docker-compose.yml`
- `app/config/settings.py`
- `app/config/proxy_env.py`
- `docker/opensandbox.toml`
- `docker/sandbox.Dockerfile`
