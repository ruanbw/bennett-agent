# Bennett Agent 前端

React 19 + Vite + Tailwind 4 + [assistant-ui](https://www.assistant-ui.com/) 的对话界面。
会话与消息全部存在浏览器 IndexedDB（Dexie）里，服务端不保存任何会话状态。

## 命令

```bash
npm install

npm run dev        # 开发服务器（:5173，/api 代理到 :8000）
npm run build      # tsc --noEmit && vite build  ← 构建前先类型检查
npm run typecheck  # 只做类型检查
npm run lint       # oxlint
npm test           # vitest run
npm run preview    # 预览构建产物（:4173）
```

`npm run build` 会先跑 `tsc --noEmit`。Vite 用 esbuild 转译、**不做类型检查**，所以
类型门禁必须单独挂在这一步上，否则 `tsc` 永远跑不到。

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

- 附件/多模态：后端 `ChatMessage` 只有文本，`/api/chat` 不接受图片。此前存在一套
  附件 UI（能贴图、能预览，但提交时被静默丢弃），已作为「假承诺」移除；
  需要多模态时从 git 历史取回 `attachment.aui.tsx` / `use-attachment-src.ts` 并补后端支持。
