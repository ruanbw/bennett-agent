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
| `postgres` | 默认 | 业务库（用户、会话、同步、沙箱租约与命令审计） |
| `redis` | 默认 | 限流计数、搜索缓存、会话热缓存 |
| `rustfs` | 默认 | S3 协议对象存储 |
| `app` | `app` | FastAPI 后端，启动时执行 `sql/init.sql`，同进程跑沙箱回收 |

开发时只跑依赖，前后端由开发者自己用命令起；生产时用 `app` profile 打开全部服务。

端口与卷（`docker compose down` 不删命名卷，加 `-v` 才删）：

| 服务 | 容器内端口 | 宿主映射 | 命名卷 |
|---|---|---|---|
| `postgres` | 5432 | `127.0.0.1:5432` | `bennett-postgres-data` |
| `redis` | 6379 | `127.0.0.1:6379` | `bennett-redis-data` |
| `rustfs` | 9000（S3 API）/ 9001（控制台） | `127.0.0.1:9000` / `127.0.0.1:9001` | `bennett-rustfs-data`、`bennett-rustfs-logs` |
| `app` | 8000 | `127.0.0.1:8000` | 无（无状态） |

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
- **`AUTH_SECRET` 同时是库里 API Key 的加密密钥**（`app/config/secret_box.py`）。轮换它等于让所有已加密的 Key 解不开，需要在管理后台逐个重填；`/health/ready` 的 `provider_keys` 项会点名。
- **连接池总量 = (`DATABASE_POOL_SIZE` + `DATABASE_MAX_OVERFLOW`) × 进程数**，默认 10 + 10 = 20。`main.py` 不起多 worker，所以单容器就是一个进程；多实例部署时乘以实例数，并确保明显小于 compose 里设的 Postgres `max_connections=200`。
- **多套部署共用一个 Redis 时 `REDIS_KEY_PREFIX` 必须各不相同**（默认 `bennett`），否则限流桶与会话缓存会互相踩。
- **服务对外暴露时设 `PROVIDER_BLOCK_PRIVATE_TARGETS=true`**（默认 `false`，因为本机 LiteLLM 网关与内网 vLLM 需要它）。无论开关如何，链路本地地址（含云厂商元数据服务）永远被拒绝。
- **私有部署关掉公开注册**：`REGISTRATION_ENABLED=false` 后注册接口返回 403「注册功能已关闭」，改用 `scripts/seed_admin.py` 建号。
- **`/docs`、`/redoc`、`/openapi.json` 在 production 默认关闭**（`ENABLE_API_DOCS` 不设即自动判断）。要开就显式设 `true`，并保证它在反代白名单之后。

## 起服务

```bash
docker compose --profile app up -d --build
docker compose logs -f app
```

后端镜像（`Dockerfile`）的特点：

- 依赖层（`uv sync --frozen --no-dev`）单独成层，排在业务代码的 `COPY` 之前——改一行 Python 不会让依赖层失效；`--frozen` 严格按 `uv.lock` 安装，`--no-dev` 把 pytest / ruff 挡在运行镜像外；
- 非 root 用户 `app`（uid 1001）运行；
- 容器内监听 `0.0.0.0`（容器里的 `127.0.0.1` 只接本机连接，宿主映射的端口进不来）；
- 健康检查用 `/health/ready`（真的 ping 数据库），不用 `/health`——liveness 做 readiness 会让「进程活着但数据库坏了」的实例留在负载均衡里；
- 入口走 `main.py` 而不是直接 `uvicorn`，为了保留日志格式（含 `[request_id]`）与 `no_proxy` 清理；
- 停止时 `stop_grace_period: 25s`：uvicorn 优雅关闭等 5s，回收循环收尾最多 10s，25s 覆盖这两段又不至于让 `docker stop` 等到超时。

生产环境**不要**用 `--reload`：它在 `APP_ENV=production` 下会被参数解析直接拒绝。

## 前端静态托管

```bash
cd frontend && npm run build
cd admin-frontend && npm run build
```

两个 `build` 都是 `tsc --noEmit && vite build`：类型错误直接让构建失败，不会产出半成品。产物在 `frontend/dist` 与 `admin-frontend/dist`（Vite 默认 `outDir`，仓库没有改），交给任意静态服务器或 CDN。两个要点：

1. **SPA fallback**：未知路径回落到 `index.html`（管理后台的 `react-router` 需要）；
2. **缓存策略**：`dist/` 里的资源带 hash，可以长缓存；`index.html` 必须不缓存。

## 反向代理

反代是生产部署里最容易出错的一环。四件事必须配对。

### 1. SSE 禁缓冲

对话接口返回 `text/event-stream`。代理默认会缓冲响应，表现为「模型在生成，但前端一直转圈直到一次性出现全部内容」。Nginx：

```nginx
location /api/chat {
    proxy_pass http://127.0.0.1:8000;
    proxy_http_version 1.1;
    proxy_set_header Connection "";
    proxy_buffering off;          # 应用已发 X-Accel-Buffering: no，这里再显式关一次
    proxy_read_timeout 900s;      # 单轮 SSE 预算 600s
}
```

几条依据：

- 应用侧已经在 SSE 响应上带 `X-Accel-Buffering: no` 与 `Cache-Control: no-store`（`app/api/chat.py` 的 `SSE_HEADERS`），Nginx 认这个头；但 CDN、云负载均衡、另一层代理不一定认，所以 location 里仍显式关缓冲。
- `proxy_http_version 1.1` 与 `Connection ""` 是必需的：默认的 HTTP/1.0 上游会让流在第一个事件后断掉。应用侧还从 SSE 响应里删掉了 `Connection: keep-alive`，因为 HTTP/2 禁止该标头，网关会重置流。
- **不要**加 `chunked_transfer_encoding off`。它让 Nginx 对客户端改用非分块编码，而 SSE 的响应长度未知，结果只能攒够再发，等于自己把流式变成一次性返回。
- 读超时必须大于单轮预算：`TOTAL_STREAM_BUDGET_SECONDS = 600`（`app/services/chat_service.py`），单次 send 超时 30s。900s 是留了余量的取值。
- `proxy_cache off` 只在同层 `server` 段开了 `proxy_cache` 时才需要写；响应本身已是 `no-store`。
- 前端会校验响应 `content-type` 含 `text/event-stream`，否则报「接口返回的不是事件流（content-type: …），可能被反向代理改写」。反代返回 200 的 HTML 错误页正好触发这条。
- `/api/sync` 的 pull 长轮询最长挂起 `SYNC_LONG_POLL_SECONDS`（默认 25s），低于 Nginx 默认的 60s 读超时，不用单独调；把它调大到 60s 以上才需要同步放宽 `proxy_read_timeout`。

### 2. 可信代理头与客户端 IP

认证限流按来源 IP 计数，反代部署时如果不配，所有请求会被算作同一个来源（全站共享一个桶）。

后端侧：

```text
TRUSTED_PROXY_HEADERS=X-Forwarded-For
TRUSTED_PROXY_IPS=127.0.0.1/32,10.0.0.0/8
```

**两项必须同时配**。只配标头名而不校验来源，等于让任何直连客户端自己填 `X-Forwarded-For` 来换限流桶（每次换一个值就绕过 10 次/分钟）；代码里这种情况是 fail closed——标头一律不采信，启动日志会打一条 WARNING。两者都配后，只有直连对端落在受信网段内才采信标头，并从右往左跳过受信代理、取第一个非代理地址。

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

三条硬约束：

- `CORS_ALLOWED_ORIGINS` 同时是**跨站请求守卫**的来源白名单（`app/middleware.py:OriginGuardMiddleware`）。同域部署留空没问题：守卫在来源与 `Host` 同 host 时放行；跨站部署漏配这一项，所有非 GET 的 `/api/*` 会 403「跨站请求被拒绝」，与 CORS 报错长得完全不同。
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
| 沙箱镜像 | 本仓库 `docker/sandbox.Dockerfile` | 构建后推到集群可拉取的镜像仓库，tag 要与 `SANDBOX_TEMPLATE_VERSION`（默认 `0.1`）对应 |

后端侧配置：

```text
SANDBOX_ENABLED=true
SANDBOX_SERVER_URL=https://sandbox.internal.example.com
SANDBOX_SERVER_API_KEY=<强随机>
```

出站拦截由 OpenSandbox server 承担：本地 `docker/opensandbox.toml` 的 `[egress]` 是 `dns` 模式（按域名放行），生产用集群侧对应配置。`.env` 里的 `SANDBOX_EGRESS` 后端代码并不读（只有 `app/config/settings.py` 里的字段定义），把它当开关会误判——真正决定能不能出网的是 server 那一侧。

`SANDBOX_SERVER_API_KEY` 生产必须配：为空时 server 会要求显式确认「无鉴权」（`OPENSANDBOX_INSECURE_SERVER=YES`），而这个确认只适用于本机开发。

OpenSandbox server 的存储在生产要用 PostgreSQL（多实例必须共享同一个库），本地 `docker/opensandbox.toml` 里的 SQLite 只服务单机。

## 数据库结构：幂等，无增量迁移

`sql/init.sql` 是数据库结构的**唯一事实来源**，整段幂等、在单个事务里逐句执行，应用启动时执行一次（`DATABASE_APPLY_SCHEMA=true`，默认）。多实例并发启动也安全：并发 DDL 由 Postgres 自己的锁串行化。

但它**不是迁移工具**：`CREATE TABLE IF NOT EXISTS` 对既有表是 no-op，**新增的列不会加到既有表上**。所以启动时还会逐列比对模型与库（`app/db/schema.py:verify_schema_at_head`），缺列直接拒绝启动，并把每个缺的列渲染成一条可复制的语句：

```text
数据库结构已漂移：模型里有这些列，库里没有——…
  ALTER TABLE <表> ADD COLUMN IF NOT EXISTS <列>;
```

本项目不维护增量迁移。改表的流程是：

- **开发库**：改 `sql/init.sql` 后直接重建——`docker compose down -v && docker compose up -d postgres`；
- **生产库**：改脚本，手工执行启动错误里给出的那几条 `ALTER TABLE`（或等价 DDL），再重启应用。只重跑 `sql/init.sql` 不会补列。

::: warning
`docker compose down -v` 会删除全部数据：Postgres 数据、Redis 数据、对象存储里的沙箱产物。不可恢复。生产环境不要执行这条命令。
:::

## 升级流程

```bash
git pull
docker compose --profile app up -d --build
docker compose logs -f app
```

步骤说明：

1. `git pull` 取到新代码与新的 `sql/init.sql`；
2. `--build` 重建后端镜像（依赖层有缓存，通常只有业务代码层失效）；
3. 新容器启动时自动执行 `sql/init.sql`：补齐缺失的表，**不会**给既有表加列。这次升级若有新增列，启动会以「数据库结构已漂移」失败并打印 `ALTER TABLE` 语句——先执行它们，再 `docker compose --profile app up -d`；
4. 前端另行 `npm run build` 并替换静态产物。

## 备份

要备份的是两处：

| 数据 | 位置 | 方式 |
|---|---|---|
| 业务数据 | Postgres 卷 `bennett-postgres-data` | `pg_dump` |
| 沙箱产物（workspace 归档与 `manifest.txt`） | 对象存储卷 `bennett-rustfs-data` / 外部 S3 桶 | 存储侧快照或 `aws s3 sync` 类工具 |

Postgres 备份示例（服务名 `postgres`，库名与用户默认都是 `bennett`）：

```bash
docker compose exec -T postgres pg_dump -U bennett -d bennett -Fc > bennett-$(date +%F).dump
```

恢复：

```bash
docker compose stop app
docker compose exec -T postgres pg_restore -U bennett -d bennett --clean < bennett-2026-01-01.dump
docker compose --profile app up -d
```

`--clean` 会先 DROP 再重建对象，所以在跑的应用会看到表消失——先停 `app` 再恢复。恢复后确认 `/health/ready` 返回 200。

Redis 不需要备份：它存的是限流计数、缓存与会话热缓存，丢失只触发降级（限流退回进程内计数、缓存 miss、会话回源数据库）。

对象存储的凭据要单独保管——丢了就无法恢复原件。

## 健康检查

| 端点 | 用途 | 语义 |
|---|---|---|
| `GET /health` | liveness | 不碰数据库与上游，失败即该重启；返回 `{"status":"ok","version":…}` |
| `GET /health/ready` | readiness | 逐项探测 `database` / `redis` / `storage` / `provider_keys`，必需依赖失败返回 503 |
| `GET /api/metrics` | Prometheus 指标 | 默认关闭（`METRICS_ENABLED=false`），关闭时返回 404 |

`/health/ready` 的 `status` 取 `ready` / `degraded` / `not_ready` 三值：`degraded` 仍是 200，只有 `required` 列表（`READINESS_REQUIRED_DEPENDENCIES`，默认 `database`）里的依赖失败才 503。Redis 挂了只降级、对象存储挂了只影响沙箱产物导出，把它们列为必需会让一次可恢复的抖动把所有实例一起摘出负载均衡。

部署到负载均衡器后面时：

- liveness → `/health`
- readiness → `/health/ready`（compose 与 Dockerfile 的 `HEALTHCHECK` 用的都是它，30s 间隔、20s `start_period`）
- 指标端点开启后应挡在内网或反代白名单后面（它不需要登录就能访问）

## 上线安全清单

发布前逐条确认（每条都对应上面某节的正文）：

- [ ] `AUTH_SECRET` 是强随机串（>= 32 字符，不含 `change-me` / `your-` 之类占位词）
- [ ] `AUTH_COOKIE_SECURE=true`，`AUTH_COOKIE_SAMESITE` 与部署形态匹配
- [ ] 跨站部署时 `CORS_ALLOWED_ORIGINS` 是明确域名（不是 `*`），且 `CORS_ALLOW_CREDENTIALS=true`
- [ ] `CORS_ALLOW_CREDENTIALS=true` 时 `AUTH_COOKIE_SAMESITE=none` 且 `AUTH_COOKIE_SECURE=true`
- [ ] `REGISTRATION_ENABLED=false`（私有部署），账号改由 `scripts/seed_admin.py` 建
- [ ] `ENABLE_API_DOCS` 与 `METRICS_ENABLED` 保持关闭，或已用反代白名单挡住
- [ ] 所有端口仍绑 `127.0.0.1`，外部访问只经带 TLS 的反代
- [ ] `PROVIDER_BLOCK_PRIVATE_TARGETS=true`（服务对外暴露时）
- [ ] `TRUSTED_PROXY_HEADERS` 与 `TRUSTED_PROXY_IPS` 同时配置（若在反代之后）
- [ ] OpenSandbox server 的出站策略是白名单（`[egress]`），`SANDBOX_SERVER_API_KEY` 已设置
- [ ] RustFS 的 `S3_ACCESS_KEY` / `S3_SECRET_KEY` 已换成随机值（默认值是公开的）
- [ ] (`DATABASE_POOL_SIZE` + `DATABASE_MAX_OVERFLOW`) × 进程数 < Postgres `max_connections`
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
- `app/api/chat.py`
- `app/api/ops.py`
- `app/db/schema.py`
- `sql/init.sql`
- `docker/opensandbox.toml`
