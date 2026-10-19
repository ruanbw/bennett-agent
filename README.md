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

首次使用点「去注册」：邮箱 + 至少 8 位密码 + 邀请码。**注册默认关闭**——部署者需先在 `.env`
里设 `REGISTRATION_INVITE_CODE`，未配置时注册接口一律 403；超管账号由 `SUPERUSER_EMAILS` 指定。

## API

| 端点 | 说明 |
|---|---|
| `POST /api/auth/register` | 注册（JSON：`email` / `password` / `invite_code`，按 IP 限流） |
| `POST /api/auth/login` | 登录（**表单编码**，字段名 `username` = 邮箱，成功 204） |
| `POST /api/auth/logout` · `GET /api/auth/me` | 登出 / 当前登录用户 |
| `GET /api/models` | 可用模型列表（含思考链、上下文窗口等元数据） |
| `POST /api/chat` · `POST /api/chat/title` | 对话（SSE 流式）/ 生成会话标题 |
| `/api/admin/*` | 提供商与模型管理、用户封禁（仅超级管理员） |

除注册与登录外所有 `/api/*` 都要求登录：凭据错误 → 400，未登录 → 401。接口文档默认只在非
production 环境暴露，需要时用 `ENABLE_API_DOCS=true` 显式打开。

## 项目结构

```text
app/          FastAPI：api / config / prompts / schemas / services / tools / db.py
frontend/     React 19 + Vite + Tailwind 4 + assistant-ui（components/admin = 管理后台）
prompts/      Jinja2 提示词模板
main.py       CLI 与 --web 入口
```

其余配置项见 `.env.example`，前端细节见 `frontend/README.md`。
