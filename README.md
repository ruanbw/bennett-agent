# Bennett Agent

基于 [PydanticAI](https://ai.pydantic.dev/) 与 [LiteLLM](https://docs.litellm.ai/) 构建的基础 Agent Loop 框架。

## 特性

- **轻量解耦的 Agent Loop**：基于 PydanticAI 实现完整的 Tool-calling 循环与上下文会话管理。
- **流式输出支持 (Streaming)**：原生支持 LLM 文本增量流式返回。
- **LiteLLM 模型适配**：通过 `LiteLLMProvider` + `OpenAIChatModel` 对接 100+ 主流大模型。
- **多环境变量管理**：基于 `pydantic-settings`，支持分层加载 `.env`、`.env.{APP_ENV}`、`.env.local`。
- **提示词抽离与模版化**：使用 Jinja2 渲染管理提示词，解耦业务逻辑与 Prompt 工程。
- **工具插件化解耦**：工具按领域独立编写，在 `app/tools` 统一导出和批量注入。
- **前后端分离**：后端只提供 `/api/*`，前端由 Vite 独立运行（接口地址写在 `frontend/.env`），改前端不必打包。
- **账号体系**：邮箱注册 + cookie 会话。会话状态存服务端（HttpOnly cookie 只带不透明 token，**不用 JWT**），因此登出/改密/踢人**即时生效**。

## 项目结构

```text
.
├── app/
│   ├── api/                # FastAPI 路由与 SSE 端点（auth.py = 认证接口）
│   ├── config/             # 配置管理 (多环境)
│   ├── prompts/            # 提示词渲染器 (PromptManager)
│   ├── schemas/            # Pydantic 请求/响应数据模型
│   ├── services/           # 核心业务服务 (对话流、模型管理、认证装配)
│   ├── tools/              # 工具函数与统一注册中心
│   ├── db.py               # SQLAlchemy 异步引擎 + user / access_token 两张表
│   └── server.py           # FastAPI 服务装配（纯 API，不托管前端产物）
├── frontend/               # React 19 + assistant-ui 前端项目（Vite 独立运行）
│   ├── .env                # 后端接口地址（留空 = 走 Vite 代理，同源）
│   └── src/                # 页面、组件、运行时与 Dexie 本地存储
├── prompts/                # 提示词模板 (Jinja2)
├── .env.example            # 环境变量模板
├── main.py                 # 入口 (支持 CLI 流式与 --web FastAPI 服务)
└── pyproject.toml
```

## 快速开始

### 1. 安装依赖

```bash
uv sync
```

### 2. 配置环境变量

复制配置模板并填入您的 LiteLLM / 目标模型配置：

```bash
cp .env.example .env.development
```

### 3. 运行 Agent

#### 终端交互模式（CLI）

```bash
# 默认开发环境
uv run python main.py

# 指定生产环境
APP_ENV=production uv run python main.py
```

#### 现代 Web 界面模式（前后端分离：FastAPI API + Vite 前端）

前端不再由后端托管，**不需要 `npm run build`**，两个进程各自启动：

1. **启动后端 API**（默认 `http://127.0.0.1:8000`，接口文档在 `/docs`）：
   ```bash
   uv run python main.py --web
   ```

2. **启动前端**（默认 `http://127.0.0.1:5173`）：
   ```bash
   cd frontend
   npm install   # 首次
   npm run dev
   ```

   打开 `http://127.0.0.1:5173`，首次使用点「去注册」创建账号（邮箱 + 至少 8 位密码）。

### 4. 认证与会话

所有 `/api/*` 端点都要求登录。首次启动会在仓库根目录自动创建 `bennett.db`（SQLite，
已被 `.gitignore` 忽略）。

| 端点 | 说明 |
|---|---|
| `POST /api/auth/register` | 注册（JSON：`email` / `password`） |
| `POST /api/auth/login` | 登录（**表单编码**：`username`=邮箱 / `password`），成功返回 204 |
| `POST /api/auth/logout` | 登出（服务端删会话，即时失效） |
| `GET /api/auth/me` | 当前登录用户，未登录返回 401 |

两个容易踩的约定：

- **登录用表单编码而不是 JSON**，字段名是 `username` 而不是 `email`。发 JSON 会得到 422。
- **登录失败是 400（凭据问题），未登录访问是 401（缺登录态）**。前端靠这个区分
  「密码错了」与「会话过期了」。

未接入邮件，因此**没有忘记密码与邮箱验证**流程（需要 SMTP）。

### 5. 为什么走 Vite 代理而不是直连后端

`frontend/.env` 的 `VITE_API_BASE_URL` 默认**留空**，请求走相对路径 `/api`，由
`vite.config.js` 的 `server.proxy` 转发到后端。这样开发期前后端**同源**，cookie 天然
可用，不需要 CORS credentials 配置。

需要从别的机器访问、或直连远端后端时，把 `VITE_API_BASE_URL` 填成完整地址，并把前端
来源加进后端白名单：

```bash
CORS_ALLOWED_ORIGINS=http://192.168.1.5:5173 uv run python main.py --web
```

注意跨域下 cookie 需要后端 `allow_credentials=True`，而 `allow_origins` **不能是 `*`**
（浏览器会直接拒绝该组合）。生产环境建议同样用反代把 `/api` 与静态资源收到同一域名。
