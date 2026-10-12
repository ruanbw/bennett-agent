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

接口地址来自 `.env` 的 `VITE_API_BASE_URL`（默认 `http://127.0.0.1:8000`）。前端不再由后端
托管，也不走 Vite 代理——后端纯 API，两者不同源。改后端端口、或从别的机器访问时改
`.env`（本地覆盖写 `.env.local`，已忽略）。

跨域由后端负责：前端来源必须在后端的 `CORS_ALLOWED_ORIGINS` 里（默认已包含
`http://localhost:5173` 与 `http://127.0.0.1:5173`），否则浏览器会拦掉请求。

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
│   └── constants.ts         # 共享常量与 id 生成
├── hooks/
│   ├── use-chat-sessions.ts # 会话 CRUD + 当前模型/等级
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
- **契约在 `types.ts` 里，与后端逐字段对齐。** 后端对应
  `app/schemas/chat.py` 与 `app/schemas/events.py`。改字段名要两端同时改。
- **不要给 markdown 加 `rehype-raw`。** `markdown-text.tsx` 只挂 `remarkGfm`，
  因此 LLM 输出里的原始 HTML 会被转义。这是当前 XSS 防护的全部依据。

## 已知未实现

- 多模态附件（见上方「附件」小节）。
- `reasoning_effort` 的可用取值来自后端的 `supported_reasoning_levels`；网关不返回该
  字段时会退化到 `low/medium/high`，并在模型明确声明 `reasoning: false` 时不展示选择器。
