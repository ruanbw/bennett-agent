# Bennett Agent 前端

React 19 + Vite + Tailwind 4 + [assistant-ui](https://www.assistant-ui.com/) 的对话界面。
会话与消息全部存在浏览器 IndexedDB（Dexie）里，服务端不保存任何会话状态。

## 命令

```bash
npm install

npm run dev        # 运行前端（:5173，接口地址取自 .env）
npm run build      # tsc --noEmit && vite build  ← 构建前先类型检查
npm run typecheck  # 只做类型检查
npm run lint       # oxlint
npm test           # vitest run
npm run preview    # 预览构建产物（:4173）
```

`npm run build` 会先跑 `tsc --noEmit`。Vite 用 esbuild 转译、**不做类型检查**，所以
类型门禁必须单独挂在这一步上，否则 `tsc` 永远跑不到。

## 后端地址

接口地址来自 `.env` 的 `VITE_API_BASE_URL`，**默认留空 = 请求相对路径 `/api`**，由
`vite.config.js` 的 `server.proxy` 转发到 `http://127.0.0.1:8000`。开发期前后端同源，
HttpOnly 会话 cookie 开箱即用，不需要任何 CORS 配置（`src/lib/api.ts` 里的
`credentials: 'include'` 在同源下与默认行为一致）。

直连后端（改端口、或从别的机器访问）时，把 `VITE_API_BASE_URL` 填成完整地址，并让后端
把前端来源加进 `CORS_ALLOWED_ORIGINS`（默认已包含 `http://localhost:5173` 与
`http://127.0.0.1:5173`）、把 `CORS_ALLOW_CREDENTIALS` 设为 true（默认 false，不开浏览器
会拦掉带 cookie 的跨域响应）；此时 `CORS_ALLOWED_ORIGINS` 不能含 `*`，否则后端拒绝启动。
本地覆盖写 `.env.local`（已忽略）。

事实边界：会话 cookie 的 `SameSite` 目前固定为 `Lax`（`app/services/auth.py`），因此只支持
跨 origin 但**同 site** 的直连（如 `localhost:5173` 调 `localhost:8000`，端口不同不影响
same-site）；真正**跨站**（不同注册域）浏览器不会带上 cookie，需要自行改成
`SameSite=None + Secure`，当前代码不支持。

`npm run build` / `npm run preview` 属于可选路径（产物交给任意静态服务器即可）；运行界面
本身**不需要打包**。

## 目录结构

```text
src/
├── lib/                     # 纯逻辑，全部有 vitest 覆盖
│   ├── chat-transport.ts    # fetch + SSE 分帧 + 错误响应解析
│   ├── chat-request-error.ts # 非 2xx 对话错误（status / detail / Retry-After）
│   ├── connection-failure.ts # 传输失败 → 中文结论 + 是否可重试
│   ├── chat-store.ts        # run 状态机 + 按会话订阅（useSyncExternalStore）
│   ├── chat-persistence.ts  # Dexie 读写形状
│   ├── chat-runs.ts         # 编排门面（传输 → 状态 → 持久化）
│   ├── chat-message.ts      # 记录/run → assistant-ui runtime 消息
│   ├── capabilities.ts      # 运行时能力探针的客户端（起步建议裁剪）
│   ├── model-identity.ts    # 模型名称 / 提供商 / 上游 ID 的展示身份
│   ├── model-selection.ts   # 当前模型是否仍在列表、默认模型是哪一条
│   ├── reasoning-effort.ts  # 思考等级词表与归一化的唯一权威
│   ├── session-search.ts    # 会话搜索：AND 词表 / 片段截取 / 高亮偏移
│   ├── session-grouping.ts  # 会话时间分组：本地日历日边界 / 组内排序 / 午夜刷新
│   ├── start-suggestions.ts # 空对话页建议池：能力裁剪与「换一批」轮转
│   ├── export-markdown.ts   # 会话导出：Markdown 生成 + 文件名清理
│   ├── local-owner.ts       # 本地缓存归属：换账号先清库
│   ├── local-stats.ts       # 本机数据统计 + 字节格式化
│   ├── toast.ts             # 动作反馈状态机（去重/上限/自动消失）
│   ├── message-time.ts      # 消息时间格式（今天/今年/跨年）
│   ├── keyboard-shortcuts.ts # 快捷键判定与平台化提示文案
│   ├── app-info.ts          # 版本号（构建注入的单一来源）
│   └── constants.ts         # 共享常量与 id 生成
├── hooks/
│   ├── use-chat-sessions.ts # 会话 CRUD + 当前模型/等级
│   ├── use-capabilities.ts  # 启动时拉一次运行时能力（失败降级为无联网搜索）
│   ├── use-session-search.ts # 搜索防抖/中止/保留旧结果
│   ├── use-midnight-refresh.ts # 过本地午夜时推进分组用的「现在」
│   ├── use-local-stats.ts   # 设置面板打开时读一次本机数据
│   ├── use-global-shortcuts.ts # 全局快捷键挂载（判定在 lib）
│   └── use-media-query.ts   # 响应式媒体查询
├── context/                 # theme / clarification
├── components/
│   ├── assistant-ui/        # assistant-ui 基元适配（部分由 aui CLI 生成）
│   ├── ui/                  # 基础组件（button / dialog / tooltip / …）
│   └── Sidebar*.tsx         # 侧边栏及其条目、批量操作栏
└── types.ts                 # 跨边界契约（与后端共享的线上契约 + 本地记录形状）
```

## 附件（暂不支持）

输入框左侧有附件按钮，可以贴图 / 选文件并预览，但**后端只接受文本**：
`ChatMessage` 没有附件字段，`/api/chat` 也只读 `type === 'text'` 的分片。

因此有附件时：

1. 缩略图上方会显示一条明确的提示（不是静默丢弃）；
2. **发送按钮被禁用**，提示与工具提示都是「附件暂不支持，请先移除」；
3. 点缩略图右上角的 × 移除后即可正常发送。

禁用而不是「只发文字 + 提示」是故意的：只加提示容易被忽略，用户会以为图发出去了。
禁用让「贴了图却丢掉了」不可能发生。

要真正支持多模态，需要两端一起改：后端 `ChatMessage` 加附件字段、`/api/chat` 接受
图片分片、模型改多模态；前端恢复 `UserMessageAttachments`（展示已发送消息上的附件，
当前被移除——发送被阻断后它永远渲染空，是可证明不可达的死代码）。

## 几条需要知道的设计约束

- **状态在模块里，不在组件里。** 生成任务的生命周期挂在 `lib/chat-store` 上，
  切换会话或组件卸载都不会中断请求。UI 通过 `useSyncExternalStore` 订阅，
  且订阅是**按会话**的——别的会话流式输出不会让当前视图重渲染。
- **待答提问是推导的，不是标记的。** `chat-message.ts:pendingClarification` 只看
  消息列表最后一条：用户回答会追加 user 消息、忽略会清掉记录上的 `clarification`，
  两种情况都让条件自然不成立。因此没有 `handled` 标记，也没有刷新恢复循环。
- **思考链不回放。** `chat-persistence.ts:loadContextMessages` 只回传 `content`。
  思考链只用于展示，回传会放大请求体积，且长度不可控——一旦超过后端的
  `MAX_THINKING_CHARS`，该会话的每次重试都会拿到 422。
- **不要只靠 hover 暴露操作。** Tailwind 4 会把 `hover:` / `group-hover:` 包进
  `@media (hover: hover)`，触屏上永远不生效。会话条目的「…」菜单、历史消息的复制 /
  重新生成都同时挂了 `pointer-coarse:`（触屏）与 `focus-within:` / `group-focus-within:`
  （键盘），否则手机用户点不到、键盘用户看不见。会话条目整行是一个铺满的
  `<button>`（内容层 `pointer-events-none` 让点击穿透），不是 `div onClick`；多选模式
  复用 `<button role="checkbox">` + `aria-checked`，原生 Enter/Space 即生效。
- **收起侧边栏不等于失去入口。** 搜索框与同步面板都长在侧边栏里，收起后它们会一起消失：
  用户既搜不了历史，也看不到同步是不是失败了。所以顶栏的胶囊工具栏（`ChatContainer`）
  必须补上这两个入口：搜索按钮把侧边栏重新展开（移动端即打开抽屉）并把光标放进输入框，
  同步直接复用 `SyncStatusButton`。搜索请求用**递增计数器**传给 `Sidebar` 而不是布尔值——
  布尔值重复设 `true` 不会再触发渲染期校正，用户关掉搜索后再点一次就打不开了。
  另外抽屉的自动聚焦只能在「焦点还不在抽屉里」时执行，否则会把搜索框的 `autoFocus` 抢走。
  账号操作（登出 / 管理后台 / 主题）同样不能只挂在侧边栏底部：`AccountBar` 收起时不渲染，
  这些入口会一起消失。菜单内容因此抽到 `AccountMenu`，侧边栏底部用「头像 + 昵称」当触发器、
  胶囊工具栏用账号图标当触发器——**内容只有一份**，两处各写一份必然漂移。
- **同步状态要能解释，也要能恢复。** 侧边栏云图标是入口而不是装饰：面板给出
  「已同步 / 待上传 / 无法同步 / 失败」的结论、上次同步时间、待上传与被拒数量，并提供
  「立即同步」与「重新上传被拒内容」。被服务端拒收的行（`too_large` / `quota_exceeded` /
  `conversation_missing`）会停在 `syncState === 'error'`，自动重试永远不会碰它们；原因
  持久化在行上的 `syncError` 字段——只留在内存里的话，刷新后用户只会看到一个没有下文的
  数字。状态优先级与全部措辞在 `lib/sync/status-view.ts`（纯逻辑，有单测），组件只负责
  渲染与触发 `retryNow` / `retryRejected`。
  **长轮询超时不等于同步失败。** pull 是 25s 的长轮询、客户端给 40s；超时多半是标签页被
  冻结/节流（定时器暂停，恢复时立刻到期），而真正的断网会很快抛 `TypeError` 或拿到 5xx。
  单次超时静默重试、**连续 3 次**才染色（`lib/sync/long-poll.ts`，有单测），否则用户会看到
  一个假的「同步失败」而实际上下一轮就成功了。成功一次即清零计数。
- **时间分组按本地日历日算，标题吸顶，过午夜会自己更新。** 「今天 / 昨天 / 7 天内 /
  30 天内」的边界用 `new Date(y, m, d - n)` 推，不从今天零点减 24 小时的整数倍：
  DST 切换日只有 23/25 小时，减固定毫秒会把边界挪一小时。组内由
  `groupSessionsByDate` 自己按最近活动倒序排一次（同步拉回的增量、批量取消置顶等路径
  不保证已排序），不依赖调用方先跑 `sortSessions`。分组标签依赖「今天是哪一天」，
  `useMidnightRefresh` 在本地午夜与标签页重新可见时推进 `now`，否则挂机过夜后昨天
  创建的会话会一直挂在「今天」下面。长列表里分组标题用 `sticky top-0` 吸顶并显示
  条数（读屏带「N 个会话」），滚到「7 天内」时仍能看出当前段。
- **搜索必须搜正文，而且要能看见命中原因。** 侧边栏搜索是「标题 + 消息正文」：标题同步
  过滤，正文由 `lib/session-search.ts` 走 `sessionId` 索引逐条扫描（只保留每个会话最新一条
  命中消息的片段），命中行在标题下方显示一行带高亮的小字。多词是 **AND**，且必须在
  **同一条消息**里都出现——片段要能自证命中原因，跨消息拼出来的命中没有东西可展示。
  **不搜 `thinking`**：思考链默认折叠且体积不可控，命中它等于给用户一个点开也找不到关键词
  的会话。**工具调用参与搜索**（名称 / 参数 / 结果，结果只搜前 20k 字符），但片段必须带
  来源标签（`工具 search_docs` / `提问`）——工具块默认折叠，不标来源用户点开也找不到
  关键词。分片匹配也意味着多词必须落在**同一片段**里，不再跨正文与提问拼。
  输入过程中保留上一次结果（仅同一串词的前后缀变化），否则每敲一个字列表都会
  先塌掉再弹回来；旧扫描用 `AbortSignal` 中止，慢查询不会覆盖新查询的结果。
  键盘流程：搜索框里 **Enter 打开第一条结果**、**↓ 进入结果列表**；列表内 ↑/↓ 逐条移动、
  **Esc 退回搜索框**（搜索仍开着，再按一次才关闭）；关闭搜索后焦点交给搜索按钮，
  不能掉到 `<body>` 上（输入框会随关闭卸载）。

- **搜索无结果要回显词表，并说明为什么没命中。** 只写「未找到相关对话」时用户不知道是
  哪个词拖累了结果（多词是 AND），也不知道搜索覆盖了哪些字段。空态用
  `SidebarSearchEmpty` 回显当前词表（`「部署」「财报」`），说明「标题、正文、工具调用；
  多个关键词需同时命中」，并给一个**清除搜索**——只清输入，面板与焦点都留着，方便立刻
  重输（关闭搜索是 Esc 的事）。
- **快捷键只认修饰键 + 物理键位，并且必须可发现。** `⌘K` / `Ctrl+K` 打开搜索，
  `⌘,` / `Ctrl+,` 打开设置，判定统一在 `lib/keyboard-shortcuts.ts`：Meta 与 Ctrl 都认、
  不认 Alt/Shift、输入法组合中不抢键、优先看 `event.code`（俄文等非 Latin 布局下
  `event.key` 不是 `k`）。入口按钮同时挂 `title`（平台化提示）与 `aria-keyshortcuts`，
  否则快捷键只有作者知道。搜索已经打开时再按 `⌘K` 要主动聚焦输入框——`autoFocus`
  只在挂载那一次生效。
- **toast 只装「动作反馈」，不装持续状态。** 同步失败、落库失败、上下文已满这些刷新后
  仍然成立的状态留在页面内联条上（会自动消失的提示会让人错过）；一次动作的结果
  （导出完成、新建会话失败）没有持久位置可放，才用 `lib/toast.ts` + `Toaster`。
  同语气同文案去重并重置倒计时、最多 3 条、错误停 8s（成功 4s）；可达性上逐条用
  `role="status"`（错误才用 `role="alert"`），容器不设 `aria-live`。
- **请求没完成时，用户要看到「为什么」和「下一步」。** fetch 的原始拒绝信息因浏览器而异
  （Chrome「Failed to fetch」、Safari「Load failed」），直接拼出来既不是中文也不可行动。
  `lib/connection-failure.ts` 按结构化字段分类：5xx / 超时 / 断网可重试，4xx 不承诺
  重试（重发结果一样），429 保留后端 `Retry-After` 的秒数，401 直接说重新登录。对话流
  失败会把结论写进消息正文并挂到 `run.error` 上——消息区因此出现「重试」按钮，刷新后
  仍在；没收到任何输出时按「本轮失败」收尾，不叠加「回复不完整」。`/api/models` 拉取
  失败也用同一套翻译，菜单空态显示可读原因而不是一句 HTTP 500。
- **重命名失败不能静默回退。** `ChatContainer` 会 `await onUpdateTitle(...)`，所以
  `App` **必须**把 `updateTitle` 的 Promise 交回去——用 `void` 吞掉它，await 会立即
  成功，失败只剩控制台一行日志、标题悄悄退回旧值（本项目真踩过）。失败时编辑器保持
  打开、错误就地显示（`role="alert"` + `aria-invalid`），按 Enter 即重试；blur 与
  submit 可能同批触发，用 ref 挡住第二次保存。输入框用 `[field-sizing:content]` 按
  内容自适应（`size` 属性作旧浏览器退路），固定 12rem 时 90 字标题只能看见一小截。
  **长度按 Unicode 码点算（100 字），不用 `maxLength`**：`maxLength` 会在浏览器层面
  静默截断粘贴内容，还会按 UTF-16 长度劈开 emoji 的代理对。现在在 `onChange` 里截断并
  提示，`size` / 计数器同一口径。**输入法组合中的 Enter 不算保存**：中文用户按 Enter
  是在选候选词，`isComposing`（及 `keyCode 229`）时阻止表单提交。
- **设置面板里的每个数字都要有出处。** 版本号来自 package.json（`vite.config.js` 的
  `define` 注入 `__APP_VERSION__`，唯一读取点是 `lib/app-info.ts`）——此前写死 `v0.1.0`
  而 package.json 是 `0.0.0`，展示的版本跟构建无关。「本机数据」里的会话数排除墓碑
  （同步用的删除标记），否则会和侧边栏列表对不上；存储占用来自
  `navigator.storage.estimate()`，是**整个 origin** 的估算值，文案必须说清这一点，
  否则用户会拿它精确对账。设置面板挂在 `App` 而不是 `AccountBar`：侧边栏收起时
  AccountBar 不渲染，顶栏胶囊工具栏的齿轮要能打开它。
- **改密 = 踢掉所有会话。** 设置面板的「修改密码」调用 `POST /api/auth/change-password`
  （验当前密码 → 新密码走与注册同一条 `validate_password` → `UserManager.update` 触发
  `on_after_update` 删掉该账号全部 access_token）。成功后客户端**不能**再假装已登录：
  `useAuth.expireSession()` 停同步、退回登录页并说明原因；本地会话数据不清（同一账号
  重新登录后历史还在，跨账号隔离由 `claimLocalData` 负责）。表单错误用 400 而不是 401——
  401 会被全局处理当成会话过期直接登出，用户就失去了原地重试的机会。表单本身：三个
  字段各自可显隐（默认密文，`aria-label` 带字段名，不能用三个同名的「显示密码」）；
  本地校验失败会**聚焦出错的输入框**并设置 `aria-invalid` / `aria-describedby`，
  错误文案用 `role=alert`；提交中锁定输入；新密码长度与「所有设备重新登录」的后果在
  提交前就写明，而不是等用户点一次才知道。
- **本地缓存只属于一个账号。** IndexedDB 按 origin 隔离，不跟账号走。登录/注册/启动
  探测确认身份后都要先 `claimLocalData(userId)`（`lib/local-owner.ts`）：归属一致保留
  历史，换了账号先清空再写新归属，然后才允许把状态切成已登录、启动同步引擎——顺序
  反过来，新账号的同步会拿新 cookie 把旧账号待推送的行推上去。归属确认失败时**不进
  主界面**（宁可不登录，也不要在别人的缓存上继续读写）。
  会话**过期（401）不清库**：401 不代表用户想换账号，清掉会把没同步的内容直接删了。
  本地库的清空会通过 `bennett:auth` 频道广播，其他标签页收到后重新探测身份并退回加载态
  （整个主界面卸载再重挂，否则旧的会话列表还会挂在内存里）。
- **导出是用户唯一能把数据带出去的出口，所以格式要诚实。** 会话菜单的「导出为
  Markdown」读**已经落库**的会话（不读内存里的 run），与刷新后看到的一致；流式中的
  半截回答也照实导出，并在文件里标「生成被停止 / 回复不完整 / 备用模型 / 本轮失败」。
  不导出思考链（默认折叠、体积不可控）；工具参数与结果各截到 1000 字符并标注原文长度；
  围栏长度按正文里最长的反引号串 +1 取，否则工具结果里的 ``` 会提前闭合代码块。
  文件名去掉路径/保留字符、截到 60 字符，空标题退回 `对话-YYYYMMDD-HHmm.md`。
  多选模式的「导出选中」生成**一个**归档文件（`Bennett-对话导出-YYYYMMDD-HHmm.md`）：
  逐个触发下载会被浏览器拦截并要求多次确认。归档里每段会话标题降为 `##`、角色标题
  降为 `###`，段落按最近活动倒序排列（不依赖选中集合的插入顺序），墓碑会话跳过。
- **契约在 `types.ts` 里，与后端逐字段对齐。** 后端对应
  `app/schemas/chat.py` 与 `app/schemas/events.py`。改字段名要两端同时改。
- **消息时间按本地日历日分档，且不靠 hover。** 今天只显示时分，今年其它日期显示
  `MM-DD HH:mm`，跨年才显示完整日期；完整（含秒）时间放 `title`。判断「今天」用的是
  日历日而不是 24 小时差——凌晨看昨晚 23:50 的消息必须显示日期，否则和「今天 00:10」
  分不清。触屏没有 hover，时间常显在消息下方（助手消息紧跟在操作按钮之后）。
- **空会话的起步建议跟随真实能力，且只填入不发送。** 文案、图标名与能力裁剪都在
  `lib/start-suggestions.ts`（纯逻辑，有 vitest 覆盖）；`/api/capabilities` 报告联网搜索
  未装配时，依赖搜索的示例整条撤掉——示例被点开却答不了，比没有示例更伤。「换一批」
  按顺序轮转（不是随机：随机两次可能抽到同一批，看起来像按钮坏了），每批 4 条、2×2
  栅格，原生 `<button>` 键盘可触发。点击只调用 `aui.composer.setText()` 填入并把焦点
  交给输入框——自动发送会产生费用、也可能不是用户本意。能力探测失败按「没有联网搜索」
  降级，不阻塞对话主流程。
- **工具调用的折叠条要能自证「在干什么」。** 只显示工具名时，`search_docs` 和
  `fetch_page` 看不出这次查的是什么。`lib/tool-call-view.ts:toolArgsPreview` 优先取主参数
  （`query` / `q` / `question` / `url` …），没有约定字段就取第一个非空字符串，再退回原文；
  空对象/空数组不显示（`{}` 只是噪音）。预览按码点截断 48 字，折叠态的 `aria-label`
  也带上它——读屏不必展开也能知道参数。
- **登录页的密码可以显隐，但默认必须隐藏。** 输入框右侧的眼睛按钮只切换 `input` 的
  `type`（值不动，不做「明文塞进另一个字段」那种实现）；按钮带 `aria-label`
  （显示密码 / 隐藏密码）、`aria-pressed` 与 `aria-controls`，点击后把焦点还给输入框。
  切换登录/注册模式时复位为隐藏——前一屏看过的密码不该在新表单里继续明文显示。
  邮箱字段加 `autoCapitalize=none` / `autoCorrect=off` / `spellCheck=false`，避免移动端
  把邮箱首字母自动大写。
- **模型菜单要标出上下文窗口，并说明「默认」与「已不可用」。** 上下文用满时的提示就是
  「换一个窗口更大的模型」，所以候选列表必须能直接看到窗口大小（`/api/models` 的
  `context_window`，网关没报时隐藏这个徽标，不要编造）。徽标复用 `formatTokens`
  （`1.0M` / `128k`），精确值在 `title` 里；触发器的 tooltip 也带上窗口，收起菜单后
  仍可确认。`is_default` 标成「默认」徽标（新会话实际会用，和临时选中的模型不是一回事）。
  当前模型被管理员停用后，`/api/models` 会移除它，但会话里仍保存旧 id：列表**非空**
  且查不到当前 id 时，触发器转琥珀色并带警告图标，菜单顶部说明「继续发送会被拒绝」并
  给出一键「切到默认模型」。列表为空（加载中/失败）时**不**下「不可用」结论。这条
  推导在 `lib/model-selection.ts`，有 vitest 覆盖。
- **上下文进度条的分母是预算上限，不是模型窗口。** 后端判定超限用的是「窗口 - 回答预留」
  再乘安全系数（`app/services/token_budget.py`），32k 窗口可能只剩 19k 放历史。用量事件
  因此带 `context_limit_tokens`，`lib/context-usage.ts` 优先拿它当分母；拿原始窗口当分母会
  把「已经用了 90% 预算」显示成「才用了 56%」。该上限属于产生用量的那个模型，切模型后
  必须丢弃并退回新窗口（新窗口也不知道就只报绝对值）。上下文已被拒绝（`context_overflow`）
  时指示器进入「已满」态且**不显示上一轮的数字**——那是失败前的旧用量，与「已满」矛盾；
  没有用量记录但已锁定时也仍要出现，否则用户只看到输入框变灰。`lib/context-usage.ts` 的
  纯函数有 vitest 覆盖（含「窗口口径 54.7% / 预算口径 97.2%」这条回归）。
- **管理后台的表单先本地拦错，再谈服务端 422。** 新增 / 编辑提供商时名称必填、接口地址
  必须是 `http://` 或 `https://` 开头（与后端一样区分大小写）；校验失败把焦点移到对应
  输入框并挂 `aria-invalid` / `aria-describedby`，而不是把 Pydantic 的
  `body.base_url: Value error…` 原样丢给管理员。API Key 输入框可显隐（默认密文，
  `aria-controls` 指向输入框）；编辑时留空 = 不修改、勾选「清除」才发空串，这条语义仍只在
  `buildProviderPayload` 一处实现。本地校验规则在 `lib/admin.ts:validateProviderDraft`，
  有 vitest 覆盖。
- **用户列表刷新时不允许对旧行操作，页码也要自己夹回最后一页。** `loading` 由「本次请求
  key」与「最近一次完成的 key」推导，而不是在 effect 里同步 `setState(true)`（会触发
  级联渲染，React 的 lint 规则也会报）。刷新中禁封禁 / 角色 / 翻页，列表 `aria-busy`
  并降透明度，底部显示「第 a–b 个，共 N 个」与「正在刷新…」；封掉最后一页唯一用户后，
  `clampUsersOffset` 把越界 offset 夹回新的最后一页并重新拉取，不会停在一个永远空着的
  页。搜索无结果时提供「清除搜索」，不用回到顶部找清空按钮。
- **移动端输入控件不得小于 16px。** iOS Safari 在焦点落到 `font-size < 16px` 的输入控件时
  会把整页放大：用户得手动缩回，视角还会错位。`ui/Input` 基元本来就是
  `text-base md:text-sm`（为此而设），但调用点用 `text-xs` / `text-sm` 覆盖掉，等于绕过它。
  所有文本输入（composer、侧边栏搜索、标题/重命名、改密、管理后台）统一
  `text-base md:<紧凑字号>`——移动端宁可字号大一点，也不要整页缩放。
- **窄屏别把「当前选了什么」藏起来，但也别让发送按钮换行。** 模型名与思考等级是用户
  唯一能确认「这条消息会怎么发出去」的依据：只留图标、或把名字截成「DeepSee…」等于
  让用户盲发。按可用宽度分档放宽（360px 起 7rem、380px 起 8rem、320px 级回退 64px），
  思考等级文字从 360px 起显示；两个选择器的 `aria-label` 都带上当前值（`选择模型：…` /
  `思考等级：low`），读屏同样能确认。工具条用 `flex-nowrap` + 选择器的 `min-w-0`：
  空间不够时**先压缩模型名**（`truncate`），思考等级不参与压缩（不许把 `low` 缩成
  「l…」），发送按钮永远留在同一行——`flex-wrap` 会先换行再考虑压缩，结果是发送独占
  第二行、输入区凭空高 34px。480px 以下隐藏两个选择器的装饰图标（CPU / 大脑），把宽度
  让给文字：390px 触屏下模型名仍能完整显示。弹层宽度随视口收窄（模型菜单
  `100vw-4.5rem`、用量弹层 `100vw-5rem`），320px 下不会从左边或右边出界。触屏目标：
  发送/停止 40px、附件 32px、三个选择器最小高度 36px（`pointer-coarse:`）。
- **不要给 markdown 加 `rehype-raw`。** `markdown-text.tsx` 只挂 `remarkGfm`，
  因此 LLM 输出里的原始 HTML 会被转义。这是当前 XSS 防护的全部依据。

## 已知未实现

- 多模态附件（见上方「附件」小节）。
- `reasoning_effort` 的可用取值来自后端的 `supported_reasoning_levels`；网关不返回该
  字段时会退化到 `low/medium/high`，并在模型明确声明 `reasoning: false` 时不展示选择器。
