---
title: 环境准备
description: 跑起 Bennett Agent 需要的工具链、端口与服务，每项给出验证命令与配置出处。
---

# 环境准备

这页列出跑起一套 Bennett Agent 需要什么：装什么、占哪些端口、每一项怎么自己验证。所有命令都可以直接复制执行。

## 工具链

| 组件 | 版本要求 | 用途 | 验证命令 |
|---|---|---|---|
| 操作系统 | Linux / macOS / Windows（WSL2） | 跑后端与 Docker | `uname -a` |
| Python | >= 3.10；仓库用 `.python-version` 锁 3.10 | 后端运行时 | `cat .python-version` |
| uv | 不锁版本（`Dockerfile` 用 `ghcr.io/astral-sh/uv:0.11`） | 依赖安装与虚拟环境 | `uv --version` |
| Node.js | >= 20.11（两个 `package.json` 的 `engines.node`） | 两个前端的构建与 dev server | `node --version` |
| Docker + Compose v2 | 有 `docker compose` 子命令即可 | 起 Postgres / Redis / RustFS，生产跑 app | `docker compose version` |
| OpenSandbox | 可选（`opensandbox>=1.1.1`） | 沙箱代码执行 | `uvx opensandbox-server --help` |

最低组合是「Python + uv + Node + Docker」：Docker 只负责三个依赖服务，后端与前端都在宿主机上直接跑。

Python 的 3.10 是硬下限而不是随手写的：`uv sync` 按 `.python-version` 取解释器（本机没有 3.10 时 uv 自己下载），而代码里确实用到了 3.10 的边界——`app/api/ops.py` 的就绪探针用 `asyncio.wait_for` 而不是 3.11 才有的 `asyncio.timeout`。所以宿主 `python3` 的版本只影响不经过 uv 的脚本，例如生成 `AUTH_SECRET` 的那行 `python3 -c`。

### 安装参考

- uv：`curl -LsSf https://astral.sh/uv/install.sh | sh`，然后 `source ~/.bashrc`（macOS 用 zsh 的改 `~/.zshrc`）。
- Node：官方安装包或 nvm。低于 `engines.node` 时 npm 只给警告，真正的失败出现在 `npm run build`。
- Docker Compose v2 是 `docker compose`（没有连字符）；`docker-compose` 是 v1。本仓库的 compose 文件用了 `profiles` 与 `depends_on.condition: service_healthy`，两者都只在 v2 生效。

## 外部依赖（可选，但对话要用）

| 依赖 | 作用 | 说明 |
|---|---|---|
| 模型网关 | 提供模型列表与推理 | 任何 OpenAI 兼容端点 + LiteLLM 路由格式的模型名，例如 `openai/gpt-4o` |
| Tavily API Key | 联网搜索工具 | 留空或仍是占位值时搜索工具不会被装配 |
| PostgreSQL 17 | 业务库 | compose 已提供（`postgres:17-alpine`） |
| Redis 7 | 限流计数、缓存、会话热缓存 | compose 已提供（`redis:7-alpine`） |
| S3 兼容存储 | 沙箱产物导出（销毁前打包 workspace） | compose 里的 RustFS，生产可换 AWS S3 / 阿里云 OSS |

对象存储只服务沙箱产物：业务侧只有这一用途（`app/services/sandbox/artifacts.py` 导出归档，`app/api/sandbox.py` 出产物清单与预签名直链），运维侧只有就绪探针里的 `storage` 一项。`SANDBOX_ENABLED=false` 时启动会整段跳过它（`app/server.py` 的 `_ensure_object_storage`）。

模型网关的地址与密钥通过 `.env` 里的 `LITELLM_API_BASE` / `LITELLM_API_KEY` 提供。首次启动（提供商注册表未初始化时）`seed_default_provider()` 用这三项写出一条「默认网关」提供商与一个默认模型，只写库、不联网；`LITELLM_API_BASE` 不是 `http(s)://` 或指向链路本地地址时跳过引导并打警告（`app/services/model_service/provider_sync.py`）。之后数据库是权威：删光提供商再重启**不会**重新引导，要恢复只能去管理后台手工添加。详见[配置手册](configuration.md)。

## 端口占用

| 端口 | 归属 | 出处 | 备注 |
|---|---|---|---|
| 8000 | 后端 HTTP | `main.py` 的 `--port` 默认值；`docker-compose.yml` 的 `app.ports` | 默认只听 `127.0.0.1` |
| 5173 | 用户端 dev server | `frontend/vite.config.js` 的 `server.port` | Vite 把 `/api` 代理到 8000 |
| 5174 | 管理后台 dev server | `admin-frontend/vite.config.js` 的 `server.port` | 与 5173 并存是常态 |
| 5432 | PostgreSQL | `docker-compose.yml` 的 `postgres.ports` | 只发布到回环 |
| 6379 | Redis | `docker-compose.yml` 的 `redis.ports` | 只发布到回环，默认无密码 |
| 9000 | 对象存储 S3 API | `docker-compose.yml` 的 `rustfs.ports` | 健康端点是 `/health` |
| 9001 | RustFS 控制台 | `docker-compose.yml` 的 `rustfs.ports` | 建桶、看对象 |
| 8080 | OpenSandbox server | `docker/opensandbox.toml` 的 `[server] port`；`SANDBOX_SERVER_URL` 的默认值 | 仅启用沙箱时需要 |

后端默认监听 `127.0.0.1`（`main.py` 的 `--host` 默认值），compose 里三个依赖服务也都显式发布到 `127.0.0.1`——去掉这个前缀等于把库和缓存开放给同网段。

另有一批动态端口不在上表：OpenSandbox 给每台沙箱容器分配宿主端口，范围取自 `docker/opensandbox.toml` 的 `[docker] port_range_min` / `port_range_max`（40000–60000），发布地址是 `127.0.0.1`。宿主防火墙要按这个区间放行或拦截。

## 逐项验证

```bash
# 仓库锁定的解释器版本（uv sync 会按它取 3.10）
cat .python-version
python3 --version

# uv
uv --version

# Node（两个 package.json 的 engines 都要求 >= 20.11）
node --version && npm --version

# Docker 与 Compose v2
docker version --format '{{.Server.Version}}'
docker compose version

# 端口是否已被占用（8000 / 5173 / 5174）
lsof -nP -iTCP:8000 -sTCP:LISTEN
lsof -nP -iTCP:5173 -sTCP:LISTEN
lsof -nP -iTCP:5174 -sTCP:LISTEN
```

依赖服务起好之后（账号与库名取 compose 的默认值 `${POSTGRES_USER:-bennett}` / `${POSTGRES_DB:-bennett}`）：

```bash
# PostgreSQL
docker compose exec postgres pg_isready -U bennett -d bennett

# Redis
docker compose exec redis redis-cli ping        # 期望 PONG

# 对象存储（S3 API 健康端点，与 rustfs 的 healthcheck 同一个地址）
curl -fsS http://127.0.0.1:9000/health
```

## 代理环境：no_proxy 必须清理

Docker Desktop（以及部分 CI 运行器）会把宿主的 `HTTP_PROXY` / `HTTPS_PROXY` 注入每个容器。若 `no_proxy` 里没有内部服务名，容器内的 `http://rustfs:9000` 会被送进宿主代理——代理不认识 `rustfs`，返回 502，而应用侧看到的报错是「确保桶存在失败（code=502）」，与「对象存储没起来」几乎一样难查。

`main.py` 在启动时调用 `app/config/proxy_env.py:sanitize_no_proxy_env()` 清理坏掉的条目（方括号 IPv6 会被 httpx 拼成非法 URL，导致所有 `trust_env` 的 HTTP 客户端在构造阶段就失败）。但**正确的做法仍是自己在 shell 里配好**：

```bash
export no_proxy="localhost,127.0.0.1,host.docker.internal,postgres,redis,rustfs"
export NO_PROXY="$no_proxy"
```

容器内这几个名字由 compose 的 `app` 服务在 `environment:` 段写死（`NO_PROXY` / `no_proxy` 已含 `postgres` / `redis` / `rustfs`，并拼接宿主已有的值）；宿主机直跑时只需要 `localhost,127.0.0.1`。

不要写网段：`no_proxy` 在 httpx 里是 URL 白名单，没有网段语义，`192.168.0.0/16` 这种条目永远匹配不上真实主机，只会在日志里收到一条警告（`app/config/proxy_env.py:unsupported_entries`）。要内网直连就逐条列出主机名或 IP。

## `.env` 最小必填项

从 `.env.example` 复制一份再改：

```bash
cp .env.example .env
```

最小集合（生产环境的强制项见[生产部署](deployment.md)）：

| 变量 | 必填 | 默认值（`app/config/settings.py`） | 说明 |
|---|---|---|---|
| `AUTH_SECRET` | 是 | 内置占位值 | 重置密码 / 邮箱验证令牌的签名密钥，同时加密库里的提供商 API Key |
| `LITELLM_API_BASE` | 首次引导需要 | `http://localhost:4000` | 模型网关地址 |
| `LITELLM_API_KEY` | 首次引导需要 | 空 | 网关密钥 |
| `MODEL_NAME` | 首次引导需要 | `openai/gpt-4o` | 默认模型，LiteLLM 路由格式 |
| `DATABASE_URL` | 否 | `postgresql+asyncpg://bennett:bennett@127.0.0.1:5432/bennett` | 只接受 `postgresql+asyncpg://`，其它驱动启动即报错 |
| `REDIS_URL` | 否 | `redis://127.0.0.1:6379/0` | Redis 不可用时降级，不是可用性单点 |
| `S3_ENDPOINT_URL` / `S3_ACCESS_KEY` / `S3_SECRET_KEY` | 否 | 端点与凭据都为空 | 只服务沙箱产物导出；不开沙箱可整段留空 |
| `TAVILY_API_KEY` | 否 | 空 | 留空或仍是占位值时搜索工具不会被装配 |

这张表里有两项会让进程直接起不来。其一是 `AUTH_SECRET`：`APP_ENV=production` 时，仍是内置占位值、含 `change-me` / `your-` 之类占位词、或短于 32 字符，配置校验直接抛错（`app/config/settings.py:_reject_placeholder_secret_in_production`）。其二是 `DATABASE_URL`：只接受 `postgresql+asyncpg://`，残留的 sqlite 串在配置层就被拒（`_require_postgres_driver`）——那种串不会报错，而是静默建出一个空库，所以必须提前挡。因为 `settings = Settings()` 在模块导入时就实例化，这两类错误的表现都是进程启动即退出，而不是跑起来之后某个请求 500。生产环境还有同类守卫：`AUTH_COOKIE_SECURE=false` 同样拒绝启动。完整清单见[配置手册](configuration.md)。

生成 `AUTH_SECRET`：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

::: warning
不要把真实密钥写进仓库里的任何文件。`.gitignore` 已忽略 `.env*`，但已经提交过的密钥需要轮换。更换 `AUTH_SECRET` 后，库里已加密的 API Key 无法解密，需要在管理后台重新填写。
:::

## 启用沙箱需要额外准备

沙箱默认关闭（`SANDBOX_ENABLED=false`）。启用需要三样东西：沙箱镜像、OpenSandbox server、后端开关。

```bash
# 1. 构建沙箱镜像（tag 要与 SANDBOX_TEMPLATE_VERSION 对应）
docker build -f docker/sandbox.Dockerfile -t bennett-sandbox:0.1 docker/

# 2. 起 OpenSandbox server（本地 Docker 运行时）
OPENSANDBOX_INSECURE_SERVER=YES uvx opensandbox-server --config docker/opensandbox.toml

# 3. .env 里改开关
# SANDBOX_ENABLED=true
# SANDBOX_SERVER_URL=http://127.0.0.1:8080
```

镜像名与 tag 必须对上 `.env` 里的 `SANDBOX_TEMPLATE`（默认 `bennett-sandbox`）与 `SANDBOX_TEMPLATE_VERSION`（默认 `0.1`）：租约上记的版本号要能对上某个真实镜像，否则建机时拉不到镜像。

`OPENSANDBOX_INSECURE_SERVER=YES` 是显式确认「本机开发、无鉴权」。生产必须给 server 配 API Key（`SANDBOX_SERVER_API_KEY`），并且只在内网暴露。步骤细节见[本地运行](quickstart.md)。

## 相关源码

- `pyproject.toml`
- `.python-version`
- `.env.example`
- `docker-compose.yml`
- `app/config/settings.py`
- `app/config/proxy_env.py`
- `app/server.py`
- `docker/opensandbox.toml`
- `docker/sandbox.Dockerfile`
