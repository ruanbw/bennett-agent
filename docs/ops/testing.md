---
title: 测试体系
description: 零基础设施的后端测试怎么跑、tests/ 与 quality/tests/ 的分工、CI 四个 job。
---

# 测试体系

这套测试的设计前提是**零基础设施**：跑 `uv run pytest` 不需要 Docker、不需要 Postgres、不需要 Redis，也不需要真实模型网关。数据库相关的用例全部改成「直接调用函数或路由处理函数，协作者传替身」，Redis 用进程内 fakeredis。这样任何一条用例都能在本地跑完，改一行代码就能立刻知道有没有打破什么。

## 后端：功能测试，零基础设施

### 原则

| 原则 | 做法 | 反例（不要写） |
|---|---|---|
| 不连数据库 | 直接调用服务层 / 路由处理函数，把 session、provider、redis 传替身 | 起一个 TestClient 连真库跑迁移 |
| 不连真实 Redis | `tests/conftest.py` 注入进程内 fakeredis | 依赖本机 6379 上有 Redis |
| 不打真实模型 | 替身上游客户端或事件生成器 | 在用例里真的调一次 LiteLLM |
| 不起沙箱 | 替身 provider，`SANDBOX_ENABLED=false` | 真建一台容器 |
| 断言行为，不断言实现 | 断言返回内容与状态码，不断言内部调用次数（除非那是契约） | 断言「某方法被调用了 3 次」 |

### tests/ 的组织

用例按被测单元命名，一个文件一个单元：`tests/test_chat_service.py`、`tests/test_rate_limit.py`、`tests/test_sync.py`。名字用 `test_<单元>.py`，与源码模块同名，方便从失败用例反查实现。

`tests/conftest.py` 是唯一的全局装配点，三件事：

1. **环境变量在导入 `app` 之前设好**（模块级，不是 fixture 里）。`Settings` 与 `engine` 都在导入时构造，晚一步设置就来不及了：`APP_ENV=test`、`SANDBOX_ENABLED=false`、`SEARCH_CACHE_BACKEND=memory`。
2. **fakeredis 注入进程内 Redis 客户端**，并把 `close_redis()` 换成空操作。限流的 Lua 脚本走 `fakeredis[lua]`（依赖 `lupa`），否则 `eval` 会报 unknown command。`close_redis()` 原实现会把 `_client` 置 `None`，下一次 `get_redis()` 就会去连真实端口。
3. **每个用例前后复位进程级单例**：限流告警节流状态与搜索缓存 LRU。

并行时环境变量 `REDIS_KEY_PREFIX` 会带上 pytest-xdist 的 worker 名（`bennett-test_gw0` 这类），避免多个 worker 撞同一个键空间。

`pyproject.toml` 里三条 pytest 配置值得记住：

- `addopts = "-q -n auto"`：默认并行（pytest-xdist）。调试单个用例或要用 `--pdb` 时加 `-n0`。
- `asyncio_mode = "auto"`：异步用例直接写 `async def`，不用逐个包 `asyncio.run`。
- `asyncio_default_*_loop_scope = "session"`：整个会话共用一个事件循环。`app/db/base.py` 的 engine 是模块级单例，连接池里的 asyncpg 连接绑在创建它的事件循环上；「每个测试一个循环」会让第二个用例拿到上一个循环的连接，报 `Future attached to a different loop`——而那个报错发生在任何断言之前，看起来像「测试框架坏了」。

`testpaths = ["tests"]`，所以裸跑 `uv run pytest` **不会**收集 `quality/tests`；评测框架自测必须显式传路径。

### 常用命令

```bash
uv run pytest                          # 全量（并行）
uv run pytest -n0 tests/test_sync.py   # 串行调试单个文件
uv run pytest -k "cursor"              # 按名字筛
uv run pytest quality/tests -q         # 评测框架自测（独立目录，见下）
```

`-n0` 关并行。调试单个用例或要用 `--pdb` 时必须加：并行下断点与输出会互相穿插，且报错栈指向的是另一个 worker。

### quality/ 的自测

评测框架的测试放在 `quality/tests/`，**不放在仓库根的 `tests/`**。它刻意不依赖 `tests/conftest.py` 注入的环境变量与 fakeredis 替身，也不连 Postgres/Redis。谁改判分器，就在同目录看到它的用例；`pytest quality/tests` 单跑即可，CI 里也用它做独立的一道闸。

根 `tests/` 覆盖应用行为（路由、服务、同步引擎），`quality/tests/` 覆盖评测框架本身，两套互不拖累。

### 前端

两个前端的脚本完全一致：

```bash
cd frontend && npm run lint       # oxlint
cd frontend && npm run typecheck  # tsc --noEmit
cd frontend && npm test           # vitest run
cd frontend && npm run build      # tsc --noEmit && vite build
```

vitest 跑的是 **node 环境的纯逻辑测试**：`environment: 'node'`，`include: ['src/**/*.test.{js,ts}']`。仓库不装 jsdom，也没有 E2E 配置，所以 `.tsx` 测试不会被收集。依赖 IndexedDB 的模块在用例里 mock db（`chat-persistence.test.ts`）；不依赖运行时上下文的组件用 `react-dom/server` 出静态 HTML。

`npm run build` 本身就先跑 `tsc --noEmit`，CI 里保留独立的 typecheck 步骤是为了让「类型错误」和「打包失败」显示成两个不同的失败项。

### CI 的四个 job

`.github/workflows/ci.yml` 有四个 job：

| job | 跑什么 | 要不要服务 |
|---|---|---|
| `backend` | `ruff check` → `ruff format --check` → `pytest` | 不需要任何 service（零基础设施） |
| `quality` | `pytest quality/tests -q` | 不要任何服务 |
| `frontend` | lint → typecheck → test → build | 只要 Node |
| `admin-frontend` | lint → typecheck → test → build | 只要 Node |

四个 job 都不启动数据库/Redis/对象存储：`tests/conftest.py` 用进程内 fakeredis，其余协作者全部传替身。CI 不提供 service，反过来逼着用例不能偷偷连真实依赖——一旦某条用例需要真实库，它会在 CI 里直接失败，而不是被一份「恰好能连上」的环境掩盖。

## 不测什么

| 不测 | 原因 | 改用什么 |
|---|---|---|
| 真实模型调用 | 花钱、慢、会漂移 | `quality/` 的 `--driver openai` 是发版前的人工动作；框架自测用 `--driver scripted` |
| 真实沙箱隔离 | 需要容器运行时 | `scripts/smoke_sandbox.py` 是端到端冒烟，不在 CI |
| 真实 Postgres / Redis / S3 行为 | 测试基座刻意不依赖它们，避免「跑个测试还要起环境」 | 用例只测逻辑；真实 SQL 语义由建库脚本与人工冒烟覆盖 |
| 端到端请求链路 | 慢且脆，失败时定位成本高 | 直接调函数，替身协作者 |
| 文档样式 | 不是行为 | 死链由 VitePress build 检出 |

`evals/` 的行为回归评测会真的调上游模型、产生费用，**不在 CI 中运行**，是发版前的人工动作。

## 新增测试落点

| 你改的东西 | 测试放哪 | 命名 |
|---|---|---|
| 后端服务层 / 路由 | `tests/` | `test_<单元>.py` |
| 评测框架（判分器、执行器、调优、安全、报告） | `quality/tests/` | `test_quality_<部件>.py` |
| 前端纯逻辑 | `frontend/src/**/` 与 `admin-frontend/src/**/` | `<模块名>.test.ts`（与源文件同目录同名） |
| 前端组件 | 同上，但只能测纯渲染逻辑 | `<组件名>.test.ts` |

后端用例用 `async def` 直接写断言；前端用例只测纯逻辑，依赖浏览器环境的部分不写。

## Lint 与格式

```bash
uv run ruff check .            # lint
uv run ruff format --check .   # 格式
uv run ruff format .           # 改格式
```

ruff 配置在 `pyproject.toml`：`target-version = "py310"`、`line-length = 120`，规则集 `E, F, I, UP, B, SIM`，isort 的 `known-first-party = ["app"]`。E501 是开启的（没有 ignore），行长由 `ruff format` 兜底，所以 `ruff format --check` 这条检查保证「行长合规」不是靠人手维护。

## 相关源码

- `tests/conftest.py`：环境变量、fakeredis 注入、状态复位
- `pyproject.toml`：pytest 与 ruff 的全部配置
- `.github/workflows/ci.yml`：四个 CI job
- `quality/tests/README.md`：为什么评测自测不放在根 `tests/`
- `frontend/vite.config.js` 与 `admin-frontend/vite.config.js`：vitest 的环境与收集范围
- `evals/harness.py`：行为断言框架的纯逻辑（刻意不导入 `app`）
