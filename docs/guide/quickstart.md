---
title: 本地运行
description: 从起依赖到登录对话的完整本地运行步骤，每步给出命令、预期结果与常见失败原因。
---

# 本地运行

这页是一套从空环境到「能登录并跟模型对话」的完整序列。按顺序执行，每步都给了预期结果；结果不符时先看该步骤末尾的排查提示。工具链版本与端口占用见[环境准备](prerequisites.md)。

## 步骤 1：起依赖服务

```bash
cd bennett-agent
docker compose up -d
```

`docker-compose.yml` 里 `app` 服务带 `profiles: ["app"]`，所以不带 `--profile` 时只起三个依赖：`postgres`、`redis`、`rustfs`。

预期输出：`postgres`、`redis`、`rustfs` 三个容器进入 running。检查健康状态：

```bash
docker compose ps
```

预期：三个容器的 STATUS 落在 `healthy`。RustFS 的 `start_period` 是 20s（健康检查打 `http://127.0.0.1:9000/health`），第一次启动会慢一点；Postgres 与 Redis 是 10s / 5s。

失败时先看日志：

```bash
docker compose logs postgres
```

只跑 Postgres 也行（其余用不到时）：`docker compose up -d postgres`。

## 步骤 2：准备 `.env`

```bash
cp .env.example .env
python3 -c 'import secrets; print(secrets.token_urlsafe(48))'
```

把生成的值填进 `.env` 的 `AUTH_SECRET`，并把 `LITELLM_API_BASE` / `LITELLM_API_KEY` / `MODEL_NAME` 改成你实际的网关信息。`.env.example` 里的 `DATABASE_URL`、`REDIS_URL`、`S3_ENDPOINT_URL` 已指向宿主机回环端口，后端在宿主机直跑时不用改。

预期：`.env` 里至少有一行非占位的 `AUTH_SECRET=`，以及能连通的上游网关地址。

dotenv 的加载顺序是 `.env` → `.env.local` → `.env.{APP_ENV}` → `.env.{APP_ENV}.local`，靠后的覆盖靠前的，shell 环境变量始终最高（`app/config/settings.py:get_env_files`）。`APP_ENV` 本身按 shell → `.env.local` → `.env` 的顺序读，缺省 `development`。

`AUTH_SECRET` 有两点要说清：

- development 下留着占位值也能启动，`APP_ENV=production` 时会直接拒绝（长度 < 32 或含 `change-me` / `your-` 之类占位词同样拒绝）；
- 它经 SHA-256 派生出 Fernet 密钥，用来加密数据库里的提供商 API Key（`app/config/secret_box.py`）。**换掉它，后台已存的 Key 就解不开**，表现为上游 401 加后台显示「未设置密钥」。

## 步骤 3：安装后端依赖

```bash
uv sync
```

预期：创建/更新 `.venv`，输出若干包安装记录。`uv.lock` 是锁文件，`uv sync` 按它安装（只有 `pyproject.toml` 变过才会重新解析）；`[dependency-groups].dev` 里的 pytest / fakeredis / ruff 也一起装进来（测试零基础设施，不需要 docker）。只跑命令的话 `uv run` 会自己同步，这步可以省。

## 步骤 4：起后端

```bash
uv run python main.py --reload
```

预期横幅（`--reload` 走的是 `app/dev_reload.py` 的 supervisor，父进程先打一份）：

```text
============================================================
 Bennett Agent 开发热重启: http://127.0.0.1:8000（文档 /docs）
 前端请另开：cd frontend && npm run dev（http://127.0.0.1:5173）
============================================================
```

紧接着子进程再打一份 `Bennett Agent API: http://127.0.0.1:8000（文档 /docs）`。`/docs` 提示是条件渲染的：`ENABLE_API_DOCS=false` 或 `APP_ENV=production` 时显示「生产环境已关闭 API 文档」。

`--reload` 监听 `app/`、`prompts/`、`main.py`，以及 `get_env_files()` 实际加载到的那几个 dotenv 文件；保存即重启子进程（防抖 500ms），**在飞的 SSE 会被掐断**。`APP_ENV=production` 下这个参数会被参数解析直接拒绝。

启动成功的两条日志（建表在引导之前）：

```text
数据库结构已就绪
模型提供商已从环境变量完成引导，可在管理后台继续维护
```

第二条只在全新库、从未配置过提供商时出现一次（`BOOTSTRAP_PROVIDER_FROM_ENV=true` 且 `LITELLM_API_BASE` 合法）。引导只写库不访问上游，模型列表仍要在管理后台点「同步模型」才拉得到。建表脚本是 `sql/init.sql`，整段幂等，每次启动都执行一遍（`DATABASE_APPLY_SCHEMA=false` 时跳过）。

## 步骤 5：验证接口

```bash
# 存活探针：不碰数据库与上游
curl -s http://127.0.0.1:8000/health
# 期望：{"status":"ok","version":"0.1.0"}（版本号来自已安装包的元数据）

# 就绪探针：真的 ping 数据库
curl -s http://127.0.0.1:8000/health/ready
```

`/health/ready` 的 `status` 只有三种取值，响应里 `database`、`redis`、`storage`、`provider_keys` 各自带状态：

| `status` | HTTP | 含义 |
|---|---|---|
| `ready` | 200 | 依赖全正常 |
| `degraded` | 200 | 有非必需依赖不可用（如 Redis 挂了，限流退回进程内计数），服务仍可用 |
| `not_ready` | 503 | `required` 里的依赖失败；默认只有 `database`（`READINESS_REQUIRED_DEPENDENCIES`） |

数据库探测超时 2 秒（`READY_DB_TIMEOUT_SECONDS`），超时按不可用算。未开沙箱时 `storage` 报 `disabled`，不会假装 ok。

API 文档在非 production 环境默认开启：

```text
http://127.0.0.1:8000/docs
```

## 步骤 6：播种初始超级管理员

```bash
uv run python scripts/seed_admin.py --email admin@example.com
```

需要 Postgres 已在跑：脚本开头自己执行一次 `sql/init.sql`，再逐列比对模型与库（缺列会报错退出，不会带着旧结构写账号）。

脚本用 `getpass` 交互提示「请输入管理员密码：」「再输入一次：」，密码至少 8 位（`MIN_PASSWORD_LENGTH`，与注册走同一条校验）。

预期输出：

```text
✓ 管理员已创建: admin@example.com  role=superadmin
  现在可以用这个邮箱 + 密码登录管理后台。
```

脚本是幂等的：

| 情形 | 行为 |
|---|---|
| 账号不存在 | 创建为 `superadmin` |
| 已是 `superadmin` | 不动 |
| 已是普通用户 / `admin` | **报错退出**（码 2），要显式加 `--promote` |
| 已是 `superadmin` 但密码丢了 | 加 `--force-reset` 重置，旧会话全部失效 |

非交互环境用 `--password` 或环境变量 `SEED_ADMIN_PASSWORD` / `SEED_ADMIN_EMAIL`，避免密码进 shell history。

## 步骤 7：起用户端前端

```bash
cd frontend
npm install
npm run dev
```

预期：`http://localhost:5173/`。首次 `npm install` 会装依赖，之后用 `npm ci` 可复现锁定版本。

开发期 Vite 把 `/api` 代理到 `http://127.0.0.1:8000`（`frontend/vite.config.js`），所以页面与接口**同源**，HttpOnly 会话 cookie 天然可用，不需要额外配 CORS。

## 步骤 8：起管理后台（可选）

```bash
cd admin-frontend
npm install
npm run dev
```

预期：`http://localhost:5174/`（同样代理 `/api`，与 5173 并存是常态）。用步骤 6 的管理员账号登录，可以维护提供商、模型与用户。

## 步骤 9：首次登录并换模型

1. 打开 `http://localhost:5173`，用注册或登录表单建账号（密码至少 8 位）。注册调 `POST /api/auth/register`，无论邮箱是否已存在都返回 202（避免变成邮箱枚举器），前端随后自动登录。新账号角色固定是普通用户，管理员身份只能由 `seed_admin.py` 播种或由超管在后台授予。
2. 登录调 `POST /api/auth/login`，请求体是**表单编码**的 `username`（填邮箱）/ `password`，不是 JSON。成功返回 204 并下发 HttpOnly cookie，名字由 `AUTH_COOKIE_NAME` 决定（默认 `bennett_session`），有效期 `SESSION_LIFETIME_SECONDS`（默认 30 天）。凭据错误返回 400；401 专指「没有有效登录态」。前端 `apiFetch` 统一带 `credentials: 'include'`，同源下 cookie 自动回传。
3. 登录后进入主界面。右上角的模型选择器会拉 `GET /api/models`（要求登录）：
   - 列表来自数据库里的提供商与模型记录（首次启动已从环境变量引导出一条「默认网关」）；
   - 若列表为空，去管理后台点「同步模型」，或确认 `LITELLM_API_BASE` 填对了。
4. 换一个模型发一条消息，SSE 会开始逐字返回。展开工具轨迹可以看到模型实际调用了什么。

在管理后台里也可以把某个模型设为默认、禁用不需要的模型。禁用比删除安全：删光提供商后不会回退到环境变量配置（引导只在「从未初始化过」时发生一次，之后数据库是权威）。

## 步骤 10（可选）：启用沙箱

```bash
# 构建沙箱镜像（tag 要与 SANDBOX_TEMPLATE_VERSION 一致，默认 0.1）
docker build -f docker/sandbox.Dockerfile -t bennett-sandbox:0.1 docker/

# 起 OpenSandbox server（另开一个终端，保持运行）
OPENSANDBOX_INSECURE_SERVER=YES uvx opensandbox-server --config docker/opensandbox.toml
```

`docker/opensandbox.toml` 是本地配置：Docker 运行时、监听 `127.0.0.1:8080`、状态存 SQLite。`OPENSANDBOX_INSECURE_SERVER=YES` 是显式确认「本机开发、无鉴权」——本地不配 API key 时 server 会要求这个确认。

然后改 `.env`：

```text
SANDBOX_ENABLED=true
SANDBOX_SERVER_URL=http://127.0.0.1:8080
```

重启后端（`--reload` 下改 `.env` 会自动重启）。用能力探针确认（这个端点要求登录）：

```bash
curl -s http://127.0.0.1:8000/api/capabilities -H "Cookie: bennett_session=<你的会话 cookie>"
```

期望 `"sandbox": true`。也可以跑端到端冒烟，它对着真实 server 建机、执行、传文件、销毁，不依赖数据库与登录态：

```bash
uv run python scripts/smoke_sandbox.py
```

## 监听地址与风险

`main.py` 的 `--host` 默认是 `127.0.0.1`，只有本机能访问。这是刻意的：注册入口默认开放（`REGISTRATION_ENABLED` 默认 `true`），绑到 `0.0.0.0` 等于把「注册 + 上游模型额度」开放给同网段。

需要局域网访问时显式放开：

```bash
uv run python main.py --reload --host 0.0.0.0
```

放开后必须同步考虑：

- `AUTH_COOKIE_SECURE` 保持 `false`（本地是 http），但这意味着会话 cookie 明文过网；
- 认证限流按来源 IP 计数，局域网里所有人共享一个桶；启动日志会打一条 WARNING 提醒这件事（`app/server.py:_warn_if_auth_rate_limit_is_global`）；
- 想用代理头区分真实 IP，需要同时配 `TRUSTED_PROXY_HEADERS` 与 `TRUSTED_PROXY_IPS`，只配前者不生效。

生产环境请在前面放一个带 TLS 的反向代理，见[生产部署](deployment.md)。

## 5 分钟验收清单

按顺序做完这六条，就算本地环境验收通过：

- [ ] `docker compose ps` 三个依赖服务都是 `healthy`
- [ ] `curl -s http://127.0.0.1:8000/health` 返回 `{"status":"ok",...}`
- [ ] `curl -s http://127.0.0.1:8000/health/ready` 返回 200 且 `status` 为 `ready` 或 `degraded`
- [ ] `http://127.0.0.1:8000/docs` 能打开并列出 `/api/auth` 等分组
- [ ] `http://localhost:5173` 能登录，并成功发出一条消息（模型有回复）
- [ ] `http://localhost:5174` 用 `seed_admin.py` 建的账号登录，能看到提供商列表

## 常见失败

| 现象 | 原因 | 处理 |
|---|---|---|
| 启动报「生产环境必须设置强随机 AUTH_SECRET」 | `APP_ENV` 被设成 `production` | 本地改回 `development`；生产见[配置手册](configuration.md) |
| 启动报「数据库结构已漂移」 | 代码里新增了列，而 `sql/init.sql` 的 `CREATE TABLE IF NOT EXISTS` 不会给既有表加列 | 按报错里打印的 `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` 执行，或重建开发库 |
| `确保桶 'bennett-artifacts' 存在失败（code=502）` | 宿主代理拦了容器内请求 | 配好 `no_proxy` 后重启 |
| `Future attached to a different loop` | 起了多个进程共用自己的连接池 | 只留一个后端进程 |
| 前端登录后立刻掉登录 | `AUTH_COOKIE_SECURE=true` 而本地是 http | 改回 `false` |
| 模型列表为空 | 网关地址错或还没同步 | 管理后台点「同步模型」，看后端日志里的网关错误 |
| 改了 `.env` 不生效 | dotenv 加载优先级 | 见[配置手册](configuration.md)的加载优先级一节 |

## 相关源码

- `main.py`
- `app/server.py`
- `app/api/ops.py`
- `app/api/auth.py`
- `app/config/settings.py`
- `app/db/schema.py`
- `scripts/seed_admin.py`
- `scripts/smoke_sandbox.py`
- `docker-compose.yml`
- `frontend/vite.config.js`
