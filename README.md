# Bennett Agent

基于 [PydanticAI](https://ai.pydantic.dev/) 与 [LiteLLM](https://docs.litellm.ai/) 的 Agent Loop 框架，
带一个完整的 Web 界面：FastAPI 后端（SSE 流式）+ React 19 前端，会话存在浏览器本地。

## 特性

- PydanticAI 的 tool-calling 循环与上下文管理，LiteLLM 适配聚合网关。
- 工具按领域独立编写、统一注册，可按需装配；支持接入 **MCP server** 扩展外部工具。
- **工具审批（HITL）**：写入与高风险工具在执行前先由用户逐条确认。
- **知识库（RAG）**：上传文档 → 分块 → 向量化 → 混合检索，由模型自行决定何时检索。
- **联网搜索**：接入 Tavily，未配置时相应工具与提示词指引一并下线。
- 上游兜底：同模型重试 → 备用模型（切换对用户可见）→ 面向用户的错误文案。
- 上下文预算管理：超限时先压缩早期对话，压完仍超才拒绝；每次压缩都会告知用户。
- 邮箱注册 + 服务端会话 cookie，三级角色（普通用户 / 管理员 / 超管）与用户封禁。
- 会话可导出为 Markdown，支持多会话合并归档；`⌘K` 搜索对话、`⌘,` 打开设置。
- 跨设备同步：本机优先，后台增量推拉，同步状态可见、失败可重试。
- 成本估算：按维护的价格表换算每轮与整个会话的 token 花费。
- 行为回归评测（`evals/`）：改提示词或换模型后跑一遍，看哪些行为变了。

## 界面

### 对话与流式回答

![对话界面](docs/screenshots/chat.png)

支持 GFM 表格、代码块与可折叠的思考链；输入框左侧是上下文用量指示器，底部选择模型与思考等级。

### 上下文用量与成本

![上下文用量与成本](docs/screenshots/context-cost.png)

用量取自上游回报的真实 token 数；成本按登记的单价换算，是估算而非账单。

### 工具审批（HITL）

![工具审批](docs/screenshots/approval.png)

外部工具执行前被拦下，逐条列出工具名、风险等级与完整参数，默认不选中任何一项；无回应按拒绝处理。

### 模型价格

![模型价格](docs/screenshots/model-prices.png)

价格在管理后台按每百万 token 维护，留空表示「未配置」，与免费的 `0` 区分。

### 设置与运行时能力

![设置面板](docs/screenshots/settings.png)

「关于系统」报告的是实际装配结果：哪些工具可用、MCP 是否连通、知识库是否启用。

## 快速开始

```bash
uv sync
cp .env.example .env.development   # 填 LiteLLM 网关地址与 API Key
uv run python main.py              # CLI 交互
uv run python main.py --web        # 后端 API（http://127.0.0.1:8000）
```

前端：

```bash
cd frontend
npm install
npm run dev                        # http://127.0.0.1:5173
```

首次使用点「去注册」：邮箱 + 至少 8 位密码即可，新账号是普通用户。初始超管用播种脚本创建（幂等）：

```bash
uv run python scripts/seed_admin.py --email admin@example.com --password '改成你的密码'
```

之后由超管在管理后台把其他账号设为管理员。

## Docker 部署

```bash
cp .env.example .env    # 至少填 AUTH_SECRET、LITELLM_API_BASE、LITELLM_API_KEY
docker compose up -d --build
```

镜像只提供 `/api/*`，**不托管前端**：前后端是分离部署的。前端 `npm run build` 的产物
交给任意静态服务器或 CDN，再把 `/api` 反代到本服务（同一个域名下最省事，见下）。

数据落在命名卷 `bennett-data`（`/data/bennett.db`），`docker compose down` 不会删除。

跨源部署必须把前端来源写进 `CORS_ALLOWED_ORIGINS`，且前端 fetch 要带
`credentials: 'include'`——会话走 HttpOnly cookie，这两项任一缺失的表现都是
「登录成功了，但下一个请求又是未登录」。

注意：`APP_ENV=production` 下 `AUTH_SECRET` 必须是至少 32 字符的强随机值且 `AUTH_COOKIE_SECURE=true`，
否则容器启动即失败。生成密钥：

```bash
python -c 'import secrets; print(secrets.token_urlsafe(48))'
```

## 项目结构

```text
app/          FastAPI：api / config / prompts / schemas / services / tools / db.py
evals/        行为回归评测：用例、断言与执行器
frontend/     React 19 + Vite + Tailwind 4 + assistant-ui（详见 frontend/README.md）
prompts/      Jinja2 提示词模板
scripts/      seed_admin.py（播种初始超管）、run_eval.py（跑评测）
main.py       CLI 与 --web 入口
```

配置项见 `.env.example`。

## 许可

MIT，见 [LICENSE](LICENSE)。
