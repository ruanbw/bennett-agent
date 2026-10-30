---
title: 生产部署
description: 用 compose app profile 部署生产服务，覆盖配置、反向代理、沙箱、备份与上线清单。
---

# 生产部署

生产形态是「compose 起后端与依赖服务 + 静态服务器托管前端 + 反向代理统一入口」。前端不进 compose：它是独立静态产物，构建与托管是独立一步。

## 编排结构

`docker-compose.yml` 用 profile 表达开发与生产的差别，服务拓扑、卷、`depends_on`、健康检查只有一份真相：

| 服务 | profile | 说明 |
|---|---|---|
| `postgres` | 默认 | 业务库（用户、会话、同步、上传记录） |
| `redis` | 默认 | 限流计数、搜索缓存、会话热缓存 |
| `rustfs` | 默认 | S3 协议对象存储 |
| `app` | `app` | FastAPI 后端，启动时执行 `sql/init.sql`，同进程跑沙箱回收 |

开发时只跑依赖，前后端由开发者自己用命令起；生产时用 `app` profile 打开全部服务。

## 生产 .env

```bash
cp .env.example .env
```

必须改的项：

| 变量 | 生产取值 | 原因 |
|---|---|---|
| `AUTH_SECRET` | 强随机串，>= 32 字符 | 占位值会让进程**拒绝启动**；它同时是 API Key 的加密密钥 |
| `AUTH_COOKIE_SECURE` | `true` | 生产走 HTTPS；为 false 也会**拒绝启动** |
| `S3_ACCESS_KEY` / `S3_SECRET_KEY` | 随机值，且与 compose 里 RustFS 的同名变量一致 | compose 的 RustFS 默认凭据是公开值，只在回环端口上暴露 |
| `DATABASE_URL` | 不用改 | compose 的 `environment:` 会覆盖成容器内地址 |
| `CORS_ALLOWED_ORIGINS` | 同域反代时留空 | production 不回退本地前端端口 |

生成密钥：

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

几个容易忽略的点：

- **compose 会用 `.env` 做插值，`environment:` 又覆盖 `env_file:`**。所以生产容器里的 `APP_ENV` 被硬编码为 `production`，不依赖 `.env` 的值——否则从 `.env.example` 复制出来的 `development` 会让全部生产守卫（拒绝占位密钥、强制 Secure cookie、关 `/docs`、收紧 CORS）一条都不生效。
- **`environment:` 里的服务地址必须覆盖 `env_file:` 里的宿主机地址**：数据库走 `postgres:5432`，Redis 走 `redis:6379`，对象存储走 `rustfs:9000`。
- **`NO_PROXY` / `no_proxy` 已自动追加 `postgres,redis,rustfs`**。不配的话容器内的 `http://rustfs:9000` 会被宿主代理拦掉，报错看起来像「对象存储没起来」。
- **密钥不要写进 compose 文件**。compose 用 `env_file:` 而不是逐条 `environment:`，正是为了不让 `AUTH_SECRET` 出现在 `docker inspect` 的容器元数据里。

## 起服务

```bash
docker compose --profile app up -d --build
docker compose logs -f app
```

后端镜像（`Dockerfile`）的特点：

- 多阶段构建，依赖层排在 `COPY . .` 之前——改一行 Python 不会让 `uv sync` 那层失效；
- `uv sync --frozen --no-dev`，严格按 `uv.lock` 安装；
- 非 root 用户 `app`（uid 1001）运行；
- 容器内监听 `0.0.0.0`（容器里的 `127.0.0.1` 只接本机连接，宿主映射的端口进不来）；
- 健康检查用 `/health/ready`（真的 ping 数据库），不用 `/health`——liveness 做 readiness 会让「进程活着但数据库坏了」的实例留在负载均衡里；
- 入口走 `main.py` 而不是直接 `uvicorn`，为了保留日志格式（含 `[request_id]`）与 `no_proxy` 清理。

生产环境**不要**用 `--reload`：它在 `APP_ENV=production` 下会被参数解析直接拒绝。

## 前端静态托管

```bash
cd frontend && npm run build
cd admin-frontend && npm run build
```

产物是各应用的 `dist/`，交给任意静态服务器或 CDN。两个要点：

1. **SPA fallback**：未知路径回落到 `index.html`（管理后台的 `react-router` 需要）；
2. **缓存策略**：`dist/` 里的资源带 hash，可以长缓存；`index.html` 必须不缓存。

## 反向代理

反代是生产部署里最容易出错的一环。四件事必须配对：

### 1. SSE 禁缓冲

对话接口返回 `text/event-stream`。代理默认会缓冲响应，表现为「模型在生成，但前端一直转圈直到一次性出现全部内容」。Nginx：

```nginx
location /api/chat {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;          # SSE 必须关
    proxy_cache off;
    proxy_read_timeout 900s;      # 单轮 SSE 预算 600s
    chunked_transfer_encoding off;
}
```

### 2. 可信代理头与客户端 IP

认证限流按来源 IP 计数，反代部署时如果不配，所有请求会被算作同一个来源（全站共享一个桶）。

后端侧：

```text
TRUSTED_PROXY_HEADERS=X-Forwarded-For
TRUSTED_PROXY_IPS=127.0.0.1/32,10.0.0.0/8
```

**两项必须同时配**。只配标头名而不校验来源，等于让任何直连客户端自己填 `X-Forwarded-For` 来换限流桶（每次换一个值就绕过 10 次/分钟）。两者都配后，只有直连对端落在受信网段内才采信标头，并从右往左跳过受信代理、取第一个非代理地址。

代理侧也要真的设置头：

```nginx
proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
proxy_set_header X-Real-IP $remote_addr;
```

应用进程的 uvicorn 以 `proxy_headers=False` 启动，客户端 IP 的判定完全由应用层的 `TRUSTED_PROXY_*` 配置控制，不依赖框架默认行为。

### 3. CORS 与 cookie

同域反代（前端与 `/api` 同一域名）时：

```text
CORS_ALLOWED_ORIGINS=          # 留空即可
CORS_ALLOW_CREDENTIALS=false
AUTH_COOKIE_SAMESITE=lax       # 或留空自动
AUTH_COOKIE_SECURE=true
```

跨站部署（前端在 `app.example.com`、后端在 `api.example.com`）时：

```text
CORS_ALLOWED_ORIGINS=https://app.example.com
CORS_ALLOW_CREDENTIALS=true
AUTH_COOKIE_SAMESITE=none
AUTH_COOKIE_SECURE=true
```

两条硬约束：

- `allow_origins` **不能是 `*`**——浏览器会直接拒绝「`*` + 凭据」的组合，应用启动时也会报错；
- `SameSite=None` **必须同时** `AUTH_COOKIE_SECURE=true`，否则启动即报错。浏览器会丢弃「SameSite=None 但没有 Secure」的 cookie，表现为「登录接口 204、下一个请求 401」。

### 4. 只绑回环

compose 里所有端口都绑 `127.0.0.1`：

```yaml
ports:
  - "127.0.0.1:8000:8000"
```

**不要去掉 `127.0.0.1:` 前缀**。注册入口是开放的，Postgres 与 Redis 没有任何 TLS 与鉴权之外的防护（Redis 默认连密码都没有）。绑到 `0.0.0.0` 等于把库、缓存与注册入口开放给同网段。要远程访问请走 SSH 隧道或带 TLS 的反代。

## 沙箱

沙箱默认关闭。生产启用需要两个独立的部分：

| 部分 | 谁负责 | 说明 |
|---|---|---|
| OpenSandbox server | 集群侧部署 | 生产用 Kubernetes 运行时 + Firecracker（独立内核）；本地用 Docker 运行时只是 cgroup 层面的自觉 |
| 沙箱镜像 | 本仓库 `docker/sandbox.Dockerfile` | 构建后推到集群可拉取的镜像仓库，版本号要与 `SANDBOX_TEMPLATE_VERSION` 对应 |

后端侧配置：

```text
SANDBOX_ENABLED=true
SANDBOX_SERVER_URL=https://sandbox.internal.example.com
SANDBOX_SERVER_API_KEY=<强随机>
SANDBOX_EGRESS=proxy
```

`SANDBOX_SERVER_API_KEY` 生产必须配：为空时 server 会要求显式确认「无鉴权」（`OPENSANDBOX_INSECURE_SERVER=YES`），而这个确认只适用于本机开发。

OpenSandbox server 的存储在生产要用 PostgreSQL（多实例必须共享同一个库），本地 `docker/opensandbox.toml` 里的 SQLite 只服务单机。

## 数据库结构：幂等，无增量迁移

`sql/init.sql` 是数据库结构的**唯一事实来源**，整段幂等、在单个事务里执行，应用启动时执行一次。多实例并发启动也安全：并发 DDL 由 Postgres 自己的锁串行化，`CREATE ... IF NOT EXISTS` 重复执行无副作用。

**本项目不维护增量迁移。** 改表直接改脚本，然后重建开发库：

```bash
docker compose down -v && docker compose up -d postgres
```

::: warning
`docker compose down -v` 会删除全部数据：Postgres 数据、Redis 数据、对象存储里的上传原件与沙箱产物。不可恢复。生产环境不要执行这条命令。
:::

生产上如果要改结构，同样是改脚本 + 手工应用（`psql -f sql/init.sql`），然后让应用重启一次——因为脚本是幂等的，重跑没有副作用。

## 升级流程

```bash
git pull
docker compose --profile app up -d --build
docker compose logs -f app
```

步骤说明：

1. `git pull` 取到新代码与新的 `sql/init.sql`；
2. `--build` 重建后端镜像（依赖层有缓存，通常只有业务代码层失效）；
3. 新容器启动时自动执行 `sql/init.sql`，结构到 head；
4. 前端另行 `npm run build` 并替换静态产物。

数据库结构不需要单独迁移步骤——这是「脚本即迁移」的直接结果。

## 备份

要备份的是两处：

| 数据 | 位置 | 方式 |
|---|---|---|
| 业务数据 | Postgres 卷 | `pg_dump` |
| 上传原件与沙箱产物 | 对象存储卷 / 外部 S3 桶 | 存储侧快照或 `aws s3 sync` 类工具 |

Postgres 备份示例：

```bash
docker compose exec -T postgres pg_dump -U bennett -d bennett -Fc > bennett-$(date +%F).dump
```

恢复：

```bash
docker compose exec -T postgres pg_restore -U bennett -d bennett --clean < bennett-2026-01-01.dump
```

Redis 不需要备份：它存的是限流计数、缓存与会话热缓存，丢失只触发降级（限流退回进程内计数、缓存 miss、会话回源数据库）。

对象存储的凭据要单独保管——丢了就无法恢复原件。

## 健康检查

| 端点 | 用途 | 语义 |
|---|---|---|
| `GET /health` | liveness | 不碰数据库与上游，失败即该重启 |
| `GET /health/ready` | readiness | 逐项探测依赖，必需依赖失败返回 503 |
| `GET /api/metrics` | Prometheus 指标 | 默认关闭（`METRICS_ENABLED=false`），关闭时返回 404 |

就绪探针的必需依赖由 `READINESS_REQUIRED_DEPENDENCIES` 决定，默认只有 `database`。Redis 挂了只降级、对象存储挂了只影响上传，把它们列为必需会让一次可恢复的抖动把所有实例一起摘出负载均衡。

部署到负载均衡器后面时：

- liveness → `/health`
- readiness → `/health/ready`
- 指标端点开启后应挡在内网或反代白名单后面（它不需要登录就能访问）

## 上线安全清单

发布前逐条确认：

- [ ] `AUTH_SECRET` 是强随机串（>= 32 字符，不含 `change-me` / `your-` 之类占位词）
- [ ] `AUTH_COOKIE_SECURE=true`，`AUTH_COOKIE_SAMESITE` 与部署形态匹配
- [ ] 跨站部署时 `CORS_ALLOWED_ORIGINS` 是明确域名（不是 `*`），且 `CORS_ALLOW_CREDENTIALS=true`
- [ ] `CORS_ALLOW_CREDENTIALS=true` 时 `AUTH_COOKIE_SAMESITE=none` 且 `AUTH_COOKIE_SECURE=true`
- [ ] `REGISTRATION_ENABLED` 按需要决定：私有部署关掉注册，只用 `scripts/seed_admin.py` 建号
- [ ] `ENABLE_API_DOCS` 与 `METRICS_ENABLED` 保持关闭（或已用反代白名单挡住）
- [ ] 所有端口仍绑 `127.0.0.1`，外部访问只经带 TLS 的反代
- [ ] `PROVIDER_BLOCK_PRIVATE_TARGETS` 按暴露面决定是否开启
- [ ] `TRUSTED_PROXY_HEADERS` 与 `TRUSTED_PROXY_IPS` 同时配置（若在反代之后）
- [ ] `SANDBOX_EGRESS=proxy`（不是 `open`），`SANDBOX_SERVER_API_KEY` 已设置
- [ ] RustFS 的 `S3_ACCESS_KEY` / `S3_SECRET_KEY` 已换成随机值（默认值是公开的）
- [ ] `DATABASE_POOL_SIZE` + `DATABASE_MAX_OVERFLOW` × worker 数 < Postgres `max_connections`
- [ ] `REDIS_KEY_PREFIX` 与其它部署不冲突
- [ ] Postgres 备份已配置并**验证过恢复**
- [ ] 前端静态产物已构建，`index.html` 不缓存，未知路径回落 `index.html`
- [ ] `/health/ready` 返回 200，前端能登录并完成一轮对话

## 相关源码

- `docker-compose.yml`
- `Dockerfile`
- `.env.example`
- `main.py`
- `app/server.py`
- `app/config/settings.py`
- `sql/init.sql`
- `docker/opensandbox.toml`
