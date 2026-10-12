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

## 项目结构

```text
.
├── app/
│   ├── api/                # FastAPI 路由与 SSE 端点
│   ├── config/             # 配置管理 (多环境)
│   ├── prompts/            # 提示词渲染器 (PromptManager)
│   ├── schemas/            # Pydantic 请求/响应数据模型
│   ├── services/           # 核心业务服务 (对话流、模型管理)
│   ├── tools/              # 工具函数与统一注册中心
│   └── server.py           # FastAPI 服务装配（纯 API，不托管前端产物）
├── frontend/               # React 19 + assistant-ui 前端项目（Vite 独立运行）
│   ├── .env                # 后端接口地址（VITE_API_BASE_URL）
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

   打开 `http://127.0.0.1:5173` 即可开始匿名对话。

前端通过 `frontend/.env` 的 `VITE_API_BASE_URL`（默认 `http://127.0.0.1:8000`）直连后端，
因此跨域是常态：改了前端端口、或从别的机器访问时，把前端来源加进后端的
`CORS_ALLOWED_ORIGINS`：

```bash
CORS_ALLOWED_ORIGINS=http://192.168.1.5:5173 uv run python main.py --web
```
