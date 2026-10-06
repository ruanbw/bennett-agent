# Bennett Agent

基于 [PydanticAI](https://ai.pydantic.dev/) 与 [LiteLLM](https://docs.litellm.ai/) 构建的基础 Agent Loop 框架。

## 特性

- **轻量解耦的 Agent Loop**：基于 PydanticAI 实现完整的 Tool-calling 循环与上下文会话管理。
- **流式输出支持 (Streaming)**：原生支持 LLM 文本增量流式返回。
- **LiteLLM 模型适配**：通过 `LiteLLMProvider` + `OpenAIChatModel` 对接 100+ 主流大模型。
- **多环境变量管理**：基于 `pydantic-settings`，支持分层加载 `.env`、`.env.{APP_ENV}`、`.env.local`。
- **提示词抽离与模版化**：使用 Jinja2 渲染管理提示词，解耦业务逻辑与 Prompt 工程。
- **工具插件化解耦**：工具按领域独立编写，在 `app/tools` 统一导出和批量注入。

## 项目结构

```text
.
├── app/
│   ├── config/             # 配置管理 (多环境)
│   ├── prompts/            # 提示词渲染器 (PromptManager)
│   └── tools/              # 工具函数与统一注册中心
├── prompts/                # 提示词模板 (Jinja2)
├── .env.example            # 环境变量模板
├── main.py                 # 入口与交互式 Agent Loop
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

#### 现代 Web 界面模式（FastAPI + React + assistant-ui）

1. **构建前端（已有预构建产物，可直接运行）**：
   ```bash
   cd frontend
   npm install
   npm run build
   cd ..
   ```

2. **启动 FastAPI Web 服务**：
   ```bash
   uv run python main.py --web
   ```
   启动后打开浏览器访问 `http://127.0.0.1:8000` 即可开始匿名对话。

3. **前端开发模式热重载（可选）**：
   - 终端 1 启动后端：`uv run python main.py --web`
   - 终端 2 启动前端：`cd frontend && npm run dev`，访问 `http://127.0.0.1:5173`。
