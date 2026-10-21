# Bennett Agent

基于 [PydanticAI](https://ai.pydantic.dev/) 与 [LiteLLM](https://docs.litellm.ai/) 构建的 Agent Loop 框架。
后端是 FastAPI（SSE 流式，只提供 `/api/*`），前端是 React 19 + assistant-ui（会话存在浏览器 IndexedDB）。

## 特性

- PydanticAI 的 tool-calling 循环与上下文会话管理，LiteLLM 通过 `LiteLLMProvider` 适配聚合网关。
- 工具在 `app/tools` 按领域独立编写、统一注册注入；工具抛异常经 `ToolFailed` 隔离成
  `outcome=failed` 的失败结果回灌模型，不打断整轮；前端与 Markdown 导出都标成「失败」而不是绿勾。
- 上游兜底分三层：同模型重试一次 → 备用模型（切换对用户可见）→ 面向用户的错误文案。
- 上下文预算：按网关的 `context_window` 估算，超限直接拒绝并锁住输入框，不把上游 400 翻译成「模型不可用」。
- 空对话页的起步建议跟随**当前装配的工具**（`GET /api/capabilities`）：没配联网搜索时
  自动撤掉搜索示例，避免点开才发现不可用；「换一批」循环轮换其余示例，点击只填入、
  不自动发送。模型菜单标出各候选的上下文窗口，便于在上下文用满时换用更大的模型。
- 上下文用量显示在发送按钮左侧：进度条以**后端预算上限**（模型窗口扣除回答预留与安全
  余量）为分母，而不是原始窗口；接近上限会标红并给出建议，上下文已满时说明原因与出路。
  已用是上游回报的**真实** prompt token，不是估算。每条消息带发送时间
  （今天只显示时分，跨天显示日期，完整时间在悬浮提示里）。
- 邮箱注册 + 服务端会话 cookie（HttpOnly 不透明 token，非 JWT），登出/改密/封禁即时生效。
  设置里可修改密码：校验当前密码后重写，并让该账号**所有会话**立即失效（包括本次），
  客户端退回登录页；旧密码随即失效。
- 会话可导出为 Markdown（正文 + 待答提问 + 本轮失败/停止/备用模型标记 + 工具调用明细，
  不包含思考链），用于备份或引用；多选模式可把选中的会话合并导出为一个归档文件。
- 快捷键：`⌘K` / `Ctrl+K` 搜索对话，`⌘,` / `Ctrl+,` 打开设置（入口按钮的提示里也写着）。
- 设置面板显示账号角色、本机数据（会话数 / 消息数 / 浏览器估算占用）与真实版本号
  （构建时取自 package.json）；侧边栏收起时也能从顶栏胶囊工具栏打开。
- 侧边栏搜索同时搜标题、**消息正文**与**工具调用**（名称/参数/结果）：命中行给出带高亮
  的片段并标出来源（工具块默认折叠），多词按与匹配（不搜折叠的思考链）。
- 本机会话缓存按账号归属：换账号登录会先清空本机缓存（会话过期不清，同一账号重新
  登录可继续同步）；其他标签页会一起重新加载身份。
- 跨设备同步：本机变更批量推送到服务端，再按不透明游标增量拉取（无变化时长轮询）。
  侧边栏的云图标是状态入口——能看到「已同步 / 待上传 / 无法同步 / 失败」、上次同步时间，
  并对被服务端拒收的内容手动重试；会话仍以本地 IndexedDB 为主，同步是后台补强。
  收起侧边栏后，顶栏胶囊工具栏里仍保留搜索、设置、账号（登出/主题/管理后台）与同步状态入口。

## 快速开始

```bash
uv sync
cp .env.example .env.development   # 填 LiteLLM 网关地址与 API Key
uv run python main.py              # CLI 交互
uv run python main.py --web        # 后端 API（http://127.0.0.1:8000）
```

CLI 交互模式内置命令：`exit` / `quit` / `q` 退出、`clear` 重置上下文、`help` / `?`
查看帮助。启动横幅会同时显示 `MODEL_NAME` 与**实际解析到的路由**——环境变量不是后台
合法路由键时 CLI 会退回数据库默认模型，只打印环境变量会让人按错误的名字排查。

```bash
cd frontend
npm install
npm run dev                        # 前端（http://127.0.0.1:5173）
```

首次使用点「去注册」：邮箱 + 至少 8 位密码即可，注册完全开放。新账号是普通用户，
不能进管理后台。

首次启动时，`LITELLM_API_BASE` / `LITELLM_API_KEY` / `MODEL_NAME` 会被引导成一条
「默认网关」提供商，让后台还没配置时也有模型可用（只写库、不联网）。**地址写错时不写库**
（`registry.validate_bootstrap_base_url`：必须是 `http(s)://` 且带主机名，且不能是链路
本地地址）。这条顺序很重要：一旦写进库，后台就成为权威、环境变量路径停用，一条笔误就会
把刚部署的实例变成「零个可用模型」；拦住之后服务继续按环境变量工作，日志里有明确的修正
提示。

初始超管用播种脚本创建（幂等，可重复执行）：

```bash
uv run python scripts/seed_admin.py --email admin@example.com --password '改成你的密码'
```

之后由超管在管理后台把别的账号设为管理员。三个角色的权限见 `app/roles.py`：
`user` 只能对话，`admin` 能进后台维护提供商与封禁用户，`superadmin` 额外能调整角色。

后台的用户检索按**字面**匹配：`%` 与 `_` 是 SQL `LIKE` 的元字符，转义前搜一个 `%` 会
列出全部账号（看起来像搜索坏了），搜 `a_b` 还会命中 `axb`。`app/api/admin.py` 的
`escape_like_pattern` 负责转义，测试覆盖 `%`、`_`、反斜杠与大小写/子串匹配。

用户列表另支持 `?status=all|active|banned` 筛选（后台是「全部 / 正常 / 已封禁」三个
分段按钮）：账号一多，「有哪些被封了」不能靠翻页找人。筛选必须发生在**计数之前**——
`total` 要是筛选后的总数，否则分页器会指向一个不存在的页数；切换档位要回到第一页，
否则会停在越界的 offset 上。

## 系统提示词与工具装配

`prompts/system_prompt.jinja` 里的「可用工具」一节与联网搜索规则都由**实际装配结果**
渲染（`app/services/agent_factory.render_system_prompt`）：没配 `TAVILY_API_KEY` 时
`tavily_search` 根本不挂，提示词里也就不再教模型「请调用 tavily_search」，而是明确写
「本轮没有联网搜索工具，不要声称查过网络」。此前模板无条件写着搜索指令，模型会去调用
一个不存在的工具——白烧一轮 token，用户看到的是「工具不存在」。
`tests/test_agent_factory.py` 锁住「挂上的工具都出现在提示词里」这条一致性。

会话标题同样是模型输出，落库前要清洗（引号、「标题：」前缀、尾随句号、换行）并按
**码点**截断——与前端 `clampTitle` 同一口径，否则同一个标题在两端长度不同。清洗与兜底
在 `app/services/title_text.py`（纯函数，`tests/test_title_text.py` 覆盖）；模型不可用或
返回空内容时退回「首条消息首行」，并去掉 Markdown 的 `#` 记号。

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
  真实 token 用量。`--baseline auto` 取**同模型 + 同思考等级 + 同上下文守卫**的最近一份
  报告做 diff；换过 `--effort` 或 `--context-guard` 后不会拿旧配置的报告硬比，
  否则「配置不同」会被误报成「新失败」。显式传 `--baseline <路径>` 时若配置不一致会
  先打印警告。其中 **新失败（newly_failed）** 是需要立刻处理的信号：某个用例从
  **非 FAIL** 变成了 FAIL——既包括 PASS→FAIL，也包括 SKIP→FAIL（依赖刚配上、一跑就
  红）。此前只认 PASS→FAIL，SKIP→FAIL 会掉在所有分类之外，命令行还会打印「行为与基线
  一致」，正好把最该注意的一类变化藏起来。
- 仍然失败但**失败原因变了**的用例单独一行（`! 仍然失败但原因变了`）：修 A 弄坏 B 时
  两次都红，只报名字会让人以为「还是老问题」。原因比对只看断言名，不比对 detail 里的
  数值；没有 `failures` 字段的老报告不参与这条判定。
- 「行为与基线一致」只在状态**与失败原因都**没变时说，且新增/缺失用例也算差异。
- 报告内自带 `schema_version`；读入时会校验版本与每条用例的 `id`/`status`，旧版或损坏
  的基线会给出「哪份文件、哪个字段」的错误并跳过对比，而不是抛一个 KeyError。
- `requires: [tavily]` 的用例在没配 `TAVILY_API_KEY` 时记为 SKIP 而不是失败——没有 key
  时这个工具根本不会被装配。
- 用例格式见 `evals/cases.yaml` 顶部注释；断言与聚合是纯逻辑（`evals/harness.py`），
  由 `tests/test_eval_harness.py` 覆盖，不需要网关。

默认会把 `CONTEXT_GUARD_MODE` 关掉：短用例永远碰不到预算，开着只会让每条用例多查一次
模型窗口。要连同预算判定一起测就加 `--context-guard`。

## API

| 端点 | 说明 |
|---|---|
| `POST /api/auth/register` | 注册（JSON：`email` / `password`，公开，建出的账号为**普通用户**） |
| `POST /api/auth/login` | 登录（**表单编码**，字段名 `username` = 邮箱，成功 204） |
| `POST /api/auth/logout` · `GET /api/auth/me` | 登出 / 当前登录用户（含 `role`） |
| `GET /api/models` | 可用模型列表（含思考链、上下文窗口等元数据） |
| `GET /api/capabilities` | 运行时能力探针（当前装配的工具、是否可联网搜索） |
| `POST /api/chat` · `POST /api/chat/title` | 对话（SSE 流式）/ 生成会话标题 |
| `POST /api/sync/push` | 批量推送本机变更（行级幂等 upsert，逐行接受或拒绝） |
| `GET /api/sync/pull` | 按不透明游标增量拉取（`timeout` > 0 时长轮询） |
| `/api/admin/*` | 提供商与模型管理、用户封禁（`admin` 及以上） |
| `PATCH /api/admin/users/{id}/role` | 设置用户角色（仅 `superadmin`） |

`/docs` 里的中文说明对**所有**端点都成立，包括 fastapi-users 自动生成的
login / logout / register：框架给的是 `Auth:Cookie.Login` / `Register:Register` 这类英文
summary 且没有 description，`app/api/auth.py:_localize_generated_routes` 会按路径改写。
登录那条还写明「请求体是表单编码而不是 JSON」「同一 IP 有限流」，注册那条写明「没有任何
请求参数能指定角色」——这两点是打开文档的人最容易踩的坑。`tests/test_server.py` 锁住
「每个端点都有中文 summary」，新增路由忘记写说明会直接变红。

除注册与登录外所有 `/api/*` 都要求登录：凭据错误 → 400，未登录 → 401。接口文档默认只在非
production 环境暴露，需要时用 `ENABLE_API_DOCS=true` 显式打开。`/docs` 按
auth / chat / capabilities / sync / admin 分组，每组都有用途与权限说明；`tests/test_server.py`
锁住「没有端点落在未声明的 tag 下」，避免新增路由时漏分组。

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

## 提示词模板

`prompts/` 下的 Jinja2 模板由 `app/prompts/manager.py` 加载：`StrictUndefined` 让漏传变量
直接报错（而不是渲染成空串），`PromptTemplateError` 是「模板本身坏了」的统一类型。
**启动时会自检**（`app/server.py:validate_prompts`）：编译全部模板 + 真渲染一次主系统
提示词，失败就中止启动并打印是哪个文件、哪一行。此前模板写坏的表现是「服务正常启动，
某位用户发第一条消息时炸成一句『请求处理失败』」——现场看不出是提示词的问题。
`PromptManager.digest()` 给出模板源码的 12 位指纹，排查「同样的提示词为什么行为变了」时
比事后翻 git 历史对时间可靠。

## 上下文预算

`app/services/token_budget.py` 在每轮请求前估算 prompt 规模，超限直接拒绝（而不是让上游
以 400 拒绝再被错译成「模型不可用」）。两条细节：

- **窗口取自同一份快照。** 对话路径在 `resolve_chat_model` 里已经解析过
  `context_window` / `max_output_tokens`，预算不再按名字查一遍库（那是一次多余的全表
  扫描，且两次读之间管理员改配置会出现「预算按旧窗口算、实际调用走新窗口」）。
  `resolve_context_budget(..., model_limits=...)` 传快照；CLI 等直接调用传 None 时仍按
  名字查。
- **「只告警一次」的两处去重。** 网关没报窗口（`_WARNED_UNKNOWN_WINDOWS`，有界 LRU）与
  锚点被拒（`_WARNED_ANCHOR_REASONS`，按原因去重）都只在首次出现时记 WARNING：前者每轮
  都会触发刷新日志，后者在客户端版本错位时会随每条消息重复出现。需要再看一次时调
  `reset_token_budget_warnings()`。

## 共享 HTTP 客户端

模型列表、提供商同步、Tavily 搜索共用 `app/services/http_client.py` 里的一个
`httpx.AsyncClient`（有连接池与 keep-alive；此前每个调用点各建一个）。连接池显式限量
（20 条，`DEFAULT_LIMITS`）：本进程只有三类调用点，httpx 默认的 100 条在上游挂掉时只会
把句柄攒得更久。客户端绑定创建它的事件循环，换 loop 会重建——重建时旧客户端**会被关掉**
（旧 loop 已关闭时会留一条 WARNING，此时任何 loop 都无法 await 它的 `aclose`，硬在当前
loop 上排任务只会抛「attached to a different loop」）。

## 代理环境变量（no_proxy）

`no_proxy` 里的方括号 IPv6（`[::1]`、`[::1]:8000`）会让 httpx 把代理白名单拼成非法 URL：
`all://*[::1]`，于是**本进程里任何** httpx 客户端在构造阶段就抛
`InvalidURL: Invalid port: ':1]'`，模型列表、聊天上游、Tavily 全部在发请求之前失败——
看起来像网关或端口问题。`app/config/proxy_env.py` 在客户端构造前把这类条目归一化成裸
IPv6（带端口的丢掉端口：`no_proxy` 是按主机绕过代理），并把「含方括号又带冒号」的无法
解析条目丢弃 + 告警（丢掉一个条目的代价只是该主机仍走代理，比全挂小得多）。

## 工具异常隔离

工具是外部边界（网络、第三方 SDK、上游返回的形状）：`app/tools/guard.py` 用 pydantic-ai
的 `on_tool_execute_error` 把意外异常降级成一条 `outcome=failed` 的工具结果，整轮继续，
模型可以换工具或如实告知用户；异常原文只进日志。`ModelRetry` 是控制流，原样上抛
（重试语义的第二道保险）。

同一个工具在一轮里失败多次时文案会升级：第 2 次起明确写「本轮已连续失败 N 次，请立即
停止调用这个工具」——只说「不要原样重复调用」时模型常换个参数再试，而工具里的 bug 与
参数无关，重试只是白烧 token 并把这一轮拖到超时。计数在 ContextVar 里按轮次累计
（`collect_tool_failures()`，由 `chat_service` 在重试循环外层开启），并发请求互不串扰。

## 联网搜索工具

`tavily_search` 只在配置了可用的 `TAVILY_API_KEY` 时才装配（占位 Key 视为未配置）。
关键词在发请求前归一化（`normalize_query`）：折叠空白与控制字符、截到 400 字符——模型
偶尔会把整段上下文当关键词发过来，换回的是 400 或一堆无关结果，白烧一次配额；空白
关键词直接拒绝，不发请求。返回给模型的正文与上游错误信息都带
「外部网页内容（不可信…）」定界符并截断，防止网页里的「忽略以上指令」被当成命令。

## 备用模型

`FALLBACK_MODEL_NAME` 让主模型在上游 5xx/429/超时/连接失败时切一次备用模型（`FallbackModel`，
只在流打开阶段切换，不会出现「正文已流出又重来」）。解析口径收敛在
`app/services/fallback_config.py`：空值、与主模型同名都视为不启用；**配了却用不上**
（模型没启用、或不在同一提供商下）会记一条 WARNING，否则「主模型 503 直接失败」在现场
看不出是配置问题——运维会以为自己有兜底。

## 会话 cookie 与跨站部署

会话是 HttpOnly cookie（`AUTH_COOKIE_NAME`，默认 `bennett_session`），`SameSite` 由
`Settings.resolved_cookie_samesite()` 决定：`CORS_ALLOW_CREDENTIALS=true`（前后端不同站）
时取 `none`，否则取 `lax`；也可以用 `AUTH_COOKIE_SAMESITE` 显式覆盖。写死 `lax` 会让跨站
`fetch` 一律不回传 cookie——登录接口 204、下一个请求 401，看着像「会话没存住」。
`SameSite=None` 必须配 `AUTH_COOKIE_SECURE=true`，否则浏览器直接丢弃 cookie，这个组合在
启动时就会报错。

## SQLite 连接参数

`app/db.py:apply_sqlite_pragmas` 在**每条连接**上设置：`foreign_keys=ON`（SQLite 默认关，
不开级联删除失效）、`journal_mode=WAL`（读写不互相阻塞）、`synchronous=NORMAL`（WAL 下
官方推荐：只在检查点 fsync，掉电最多丢最后几个事务；默认 `FULL` 每次提交都 fsync，会拖慢
流式落库，`OFF` 则真会坏库）、`busy_timeout=5000`。

`PRAGMA journal_mode=WAL` **可能静默失败**：数据库文件放在网络文件系统（NFS/SMB）或不支持
的容器卷上时会退回 `delete`，一切照常工作、只是并发读写开始互相阻塞（表现为「回答流式中
查列表会卡住」）。启动时会 `check_journal_mode()` 复查一次，不是 `wal` 就打出明确告警——
把 README 里「不要把 SQLite 放在 NFS 上」那条从约定变成可观测的信号。

## 数据库结构漂移

项目**没有 Alembic**：`create_all` 只建缺失的表，对已存在的表一个字节都不改。所以给
已有表加列必须手写一段幂等迁移（目前只有 `_migrate_user_role_column`）。为降低「忘了写
迁移」的代价，启动时会做一次结构比对（`app/db.py:diff_schema`）：库里缺列时在日志里
点名是哪张表的哪一列，而不是等某个查询在运行期报 `no such column`。

**另一个方向也会报：库比代码更严格。** `diff_column_constraints` 找出「模型允许 NULL、
库里是 `NOT NULL`」的列——应用会按模型写 NULL，然后在运行期撞
`IntegrityError: NOT NULL constraint failed`，而这条路径往往只在某个可选字段真的有值时
才走到（例如 `conversation.model` 为空的新会话），现场极难复现。反方向（模型 NOT NULL、
库里可空）刻意不报：历史行本来就是 NULL，报出来只是噪音。

## 日志

`LOG_LEVEL`（默认 INFO）与 `LOG_ACCESS`（默认关闭）控制输出，配置在
`app/config/logging_setup.py`：

- uvicorn 会在自己的 `dictConfig` 里给 `uvicorn` / `uvicorn.error` / `uvicorn.access`
  装上 handler 且 `propagate=False`，所以只调 `logging.basicConfig` 会出现两套格式；
  这里显式替换这三个 logger 的 handler。
- 访问日志默认关闭：SSE 长连接与前端轮询会把它变成噪音，真正的 WARNING 会被淹没。
- **DEBUG 下第三方库仍压在 WARNING**：httpx / openai 会逐条打印请求头，里面就是
  `Authorization: Bearer <key>`——那不是「日志多」，而是把凭据写进日志文件。
