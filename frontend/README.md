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
│   ├── chat-store.ts        # run 状态机 + 按会话订阅（useSyncExternalStore）
│   ├── chat-persistence.ts  # Dexie 读写形状
│   ├── chat-runs.ts         # 编排门面（传输 → 状态 → 持久化）
│   ├── chat-message.ts      # 记录/run → assistant-ui runtime 消息
│   ├── reasoning-effort.ts  # 思考等级词表与归一化的唯一权威
│   ├── session-search.ts    # 会话搜索：AND 词表 / 片段截取 / 高亮偏移
│   ├── export-markdown.ts   # 会话导出：Markdown 生成 + 文件名清理
│   ├── local-owner.ts       # 本地缓存归属：换账号先清库
│   ├── local-stats.ts       # 本机数据统计 + 字节格式化
│   ├── app-info.ts          # 版本号（构建注入的单一来源）
│   └── constants.ts         # 共享常量与 id 生成
├── hooks/
│   ├── use-chat-sessions.ts # 会话 CRUD + 当前模型/等级
│   ├── use-session-search.ts # 搜索防抖/中止/保留旧结果
│   ├── use-local-stats.ts   # 设置面板打开时读一次本机数据
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
- **同步状态要能解释，也要能恢复。** 侧边栏云图标是入口而不是装饰：面板给出
  「已同步 / 待上传 / 无法同步 / 失败」的结论、上次同步时间、待上传与被拒数量，并提供
  「立即同步」与「重新上传被拒内容」。被服务端拒收的行（`too_large` / `quota_exceeded` /
  `conversation_missing`）会停在 `syncState === 'error'`，自动重试永远不会碰它们；原因
  持久化在行上的 `syncError` 字段——只留在内存里的话，刷新后用户只会看到一个没有下文的
  数字。状态优先级与全部措辞在 `lib/sync/status-view.ts`（纯逻辑，有单测），组件只负责
  渲染与触发 `retryNow` / `retryRejected`。
- **搜索必须搜正文，而且要能看见命中原因。** 侧边栏搜索是「标题 + 消息正文」：标题同步
  过滤，正文由 `lib/session-search.ts` 走 `sessionId` 索引逐条扫描（只保留每个会话最新一条
  命中消息的片段），命中行在标题下方显示一行带高亮的小字。多词是 **AND**，且必须在
  **同一条消息**里都出现——片段要能自证命中原因，跨消息拼出来的命中没有东西可展示。
  **不搜 `thinking`**：思考链默认折叠且体积不可控，命中它等于给用户一个点开也找不到关键词
  的会话。输入过程中保留上一次结果（仅同一串词的前后缀变化），否则每敲一个字列表都会
  先塌掉再弹回来；旧扫描用 `AbortSignal` 中止，慢查询不会覆盖新查询的结果。
- **设置面板里的每个数字都要有出处。** 版本号来自 package.json（`vite.config.js` 的
  `define` 注入 `__APP_VERSION__`，唯一读取点是 `lib/app-info.ts`）——此前写死 `v0.1.0`
  而 package.json 是 `0.0.0`，展示的版本跟构建无关。「本机数据」里的会话数排除墓碑
  （同步用的删除标记），否则会和侧边栏列表对不上；存储占用来自
  `navigator.storage.estimate()`，是**整个 origin** 的估算值，文案必须说清这一点，
  否则用户会拿它精确对账。设置面板挂在 `App` 而不是 `AccountBar`：侧边栏收起时
  AccountBar 不渲染，顶栏胶囊工具栏的齿轮要能打开它。
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
- **契约在 `types.ts` 里，与后端逐字段对齐。** 后端对应
  `app/schemas/chat.py` 与 `app/schemas/events.py`。改字段名要两端同时改。
- **不要给 markdown 加 `rehype-raw`。** `markdown-text.tsx` 只挂 `remarkGfm`，
  因此 LLM 输出里的原始 HTML 会被转义。这是当前 XSS 防护的全部依据。

## 已知未实现

- 多模态附件（见上方「附件」小节）。
- `reasoning_effort` 的可用取值来自后端的 `supported_reasoning_levels`；网关不返回该
  字段时会退化到 `low/medium/high`，并在模型明确声明 `reasoning: false` 时不展示选择器。
