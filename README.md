# Bennett Agent

基于 [PydanticAI](https://ai.pydantic.dev/) 与 [LiteLLM](https://docs.litellm.ai/) 构建的 Agent Loop 框架。
后端是 FastAPI（SSE 流式，只提供 `/api/*`），前端是 React 19 + assistant-ui（会话存在浏览器 IndexedDB）。

## 特性

- PydanticAI 的 tool-calling 循环与上下文会话管理，LiteLLM 通过 `LiteLLMProvider` 适配聚合网关。
- 工具在 `app/tools` 按领域独立编写、统一注册注入；工具抛异常隔离成「失败结果」回灌，不打断整轮。
- 上游兜底分三层：同模型重试一次 → 备用模型（切换对用户可见）→ 面向用户的错误文案。
- 上下文预算：按网关的 `context_window` 估算，超限直接拒绝并锁住输入框，不把上游 400 翻译成「模型不可用」。
- 每条回答下方显示上游回报的**真实** token 用量，不是估算。
- 邮箱注册 + 服务端会话 cookie（HttpOnly 不透明 token，非 JWT），登出/改密/封禁即时生效。
- 跨设备同步：本机变更批量推送到服务端，再按不透明游标增量拉取（无变化时长轮询）。
  侧边栏的云图标是状态入口——能看到「已同步 / 待上传 / 无法同步 / 失败」、上次同步时间，
  并对被服务端拒收的内容手动重试；会话仍以本地 IndexedDB 为主，同步是后台补强。

## 快速开始

```bash
uv sync
cp .env.example .env.development   # 填 LiteLLM 网关地址与 API Key
uv run python main.py              # CLI 交互
uv run python main.py --web        # 后端 API（http://127.0.0.1:8000）
```

```bash
cd frontend
npm install
npm run dev                        # 前端（http://127.0.0.1:5173）
```

首次使用点「去注册」：邮箱 + 至少 8 位密码即可，注册完全开放。新账号是普通用户，
不能进管理后台。

初始超管用播种脚本创建（幂等，可重复执行）：

```bash
uv run python scripts/seed_admin.py --email admin@example.com --password '改成你的密码'
```

之后由超管在管理后台把别的账号设为管理员。三个角色的权限见 `app/roles.py`：
`user` 只能对话，`admin` 能进后台维护提供商与封禁用户，`superadmin` 额外能调整角色。

## 评测

`evals/` 是一套**行为回归**用例：改提示词、换模型、动工具定义之后跑一遍，就知道哪些
已经能工作的行为变了。判分只看工具调用序列与硬性边界（该不该用工具、用哪一个、
澄清选项几个、有没有泄露系统提示词），不比文本相似度。

```bash
uv run python scripts/run_eval.py --list           # 看用例，不连网关
uv run python scripts/run_eval.py                  # 全量，自动与同模型上一次对比
uv run python scripts/run_eval.py --tag clarify    # 按标签过滤
uv run python scripts/run_eval.py --baseline none  # 第一次跑：建立基线
```

- 会真实调用上游模型并**产生费用**，因此不在 CI 里运行。
- 报告落在 `evals/runs/<模型>/<时间戳>.json`（已忽略），含每轮完整回答、工具入参与
  真实 token 用量。`--baseline auto` 取同模型最近一份报告做 diff，其中 **新失败
  （newly_failed）** 是唯一需要立刻处理的信号：它表示某个用例从 PASS 变成了 FAIL。
- `requires: [tavily]` 的用例在没配 `TAVILY_API_KEY` 时记为 SKIP 而不是失败——没有 key
  时这个工具根本不会被装配。
- 用例格式见 `evals/cases.yaml` 顶部注释；断言与聚合是纯逻辑（`evals/harness.py`），
  由 `tests/test_eval_harness.py` 覆盖，不需要网关。

默认会把 `CONTEXT_GUARD_MODE` 关掉：短用例永远碰不到预算，开着只会让每条用例多查一次
模型窗口。要连同预算判定一起测就加 `--context-guard`。

## API

| 端点 | 说明 |
|---|---|
| `POST /api/auth/register` | 注册（JSON：`email` / `password`，公开，建出的账号为普通用户） |
| `POST /api/auth/login` | 登录（**表单编码**，字段名 `username` = 邮箱，成功 204） |
| `POST /api/auth/logout` · `GET /api/auth/me` | 登出 / 当前登录用户（含 `role`） |
| `GET /api/models` | 可用模型列表（含思考链、上下文窗口等元数据） |
| `POST /api/chat` · `POST /api/chat/title` | 对话（SSE 流式）/ 生成会话标题 |
| `POST /api/sync/push` | 批量推送本机变更（行级幂等 upsert，逐行接受或拒绝） |
| `GET /api/sync/pull` | 按不透明游标增量拉取（`timeout` > 0 时长轮询） |
| `/api/admin/*` | 提供商与模型管理、用户封禁（`admin` 及以上） |
| `PATCH /api/admin/users/{id}/role` | 设置用户角色（仅 `superadmin`） |

除注册与登录外所有 `/api/*` 都要求登录：凭据错误 → 400，未登录 → 401。接口文档默认只在非
production 环境暴露，需要时用 `ENABLE_API_DOCS=true` 显式打开。

## 项目结构

```text
app/          FastAPI：api / config / prompts / schemas / services / tools / db.py
evals/        行为回归评测：用例（cases.yaml）/ 断言与报告（harness.py）/ 执行器（runner.py）
frontend/     React 19 + Vite + Tailwind 4 + assistant-ui（components/admin = 管理后台）
prompts/      Jinja2 提示词模板
scripts/      seed_admin.py（播种初始超管）、run_eval.py（跑评测）
main.py       CLI 与 --web 入口
```

其余配置项见 `.env.example`，前端细节见 `frontend/README.md`。
