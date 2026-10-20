/**
 * 跨边界契约与本地持久化形状。
 *
 * 这里放「不属于任何单一模块」的类型：
 *   - 与后端共享的线上契约（`GET /api/models` 响应、SSE 事件联合）；
 *   - IndexedDB 的记录形状；
 *   - 内存里一次生成的状态形状。
 *
 * 后端侧的对应定义：
 *   - `app/schemas/chat.py`（ChatMessage / ModelItem / ModelListResponse）
 *   - `app/schemas/events.py`（ChatEvent 判别联合）
 *
 * 字段名与后端保持 snake_case（线上契约）或 camelCase（本地形状），不强行统一：
 * 前者改了要两端同时改，后者是本地的。
 */

import type { ThreadMessageLike } from '@assistant-ui/react';

// =====================================================================
// GET /api/models
// =====================================================================
export interface ModelItem {
  id: string;
  name: string;
  provider: string;
  /**
   * 上游真实模型 ID（`id` 去掉 `{provider.slug}/` 前缀的部分）。
   *
   * 同一提供商下可能有多个路由指向同名模型，选择器要靠它把「看起来一模一样」
   * 的条目区分开；旧版网关路径没有这个字段。
   */
  model_id?: string | null;
  description?: string | null;
  /** 三态：true=支持、false=明确不支持、null=网关未声明（前端按“未知”回退默认等级）。 */
  reasoning: boolean | null;
  supported_reasoning_levels: string[];
  context_window?: number | null;
  max_output_tokens?: number | null;
  is_default: boolean;
}

export interface ModelListResponse {
  models: ModelItem[];
  default_model: string;
}

// =====================================================================
// SSE 事件（app/schemas/events.py 的镜像）
// =====================================================================
/**
 * 客户端合成的事件：流没有干净结束。
 *
 * 两种触发条件：
 *   1. 没收到**终止事件**（`done` 或 `error`）——连接被中断或对端异常退出；
 *   2. 收到过无法解析的帧——即使后文又来了 `done`，内容也已经缺了一块。
 *
 * 服务端契约是「以 `done` 或 `error` 收尾」，两者都是合法终止。只认 `done` 会把
 * 正常的错误路径误判成「连接中断」：用户先看到错误，又看到一句「连接已中断」——
 * 后者是错的，而且是重复提示。实测真实字节时发现过这个缺陷。
 */
export const TRANSPORT_INCOMPLETE = 'transport_incomplete';

export interface TransportIncompleteEvent {
  type: typeof TRANSPORT_INCOMPLETE;
  /** 是否丢过无法解析的帧——决定提示文案的严重程度。 */
  damaged: boolean;
}

export interface ContentDeltaEvent {
  type: 'content';
  delta: string;
}

export interface ThinkingDeltaEvent {
  type: 'thinking';
  delta: string;
}

export interface ToolCallEvent {
  type: 'tool_call';
  tool_call_id: string;
  tool_name: string;
  /** 服务端保持为字符串：展示层决定怎么格式化。 */
  args: string;
}

export interface ToolResultEvent {
  type: 'tool_result';
  tool_call_id: string;
  tool_name: string;
  result: string;
}

export interface ClarificationEvent {
  type: 'clarification';
  tool_call_id: string;
  question: string;
  options: string[];
  allow_custom_input: boolean;
}

export interface FallbackEvent {
  type: 'fallback';
  /** 失败的主模型 id（网关路由格式）。 */
  from_model: string;
  /** 实际完成本轮回答的备用模型 id。 */
  to_model: string;
}

export interface DoneEvent {
  type: 'done';
}

/**
 * 本轮的**真实** token 用量（服务端从上游响应里取，不是估算）。
 *
 * `contextTokens` 是「这次请求占了多少上下文」，也就是上下文长度的唯一准确值；
 * `contextWindow` 是那个模型的窗口（网关没报时为 null），两者相除就是进度条的含义。
 * 上游没回用量时这个事件不发，因此字段全是必需而非可选。
 */
export interface UsageEvent {
  type: 'usage';
  context_tokens: number;
  context_window?: number | null;
  output_tokens: number;
  cache_read_tokens: number;
}

/**
 * 失败分类。
 *
 * `context_overflow`：上下文超出模型窗口。重试必然同样失败（历史只会更长），
 * 用户只能新建会话或换窗口更大的模型——所以既不给「重试」按钮，也不能只说
 * 「出错了」。
 */
export type ErrorCode = 'context_overflow';

export interface StreamErrorEvent {
  type: 'error';
  /** 只含可以外泄的文案（校验原文或错误编号）；内部异常只给关联 ID。 */
  message: string;
  /**
   * 是否值得让用户手动重试（决定是否显示「重试」按钮）。
   *
   * 可选：旧服务端不返回该字段，视为 `false`——宁可不给按钮，也不要给一个必然失败的
   * 按钮。服务端自动重试是另一套判定，与此无关。
   */
  retryable?: boolean;
  /**
   * 失败分类（机器可读）。`type` 只告诉我们「这是一个 error」而区分不了「哪一种失败」，
   * 靠 `message` 里的中文做判断等于把文案当成协议。
   *
   * 可选：旧服务端不返回该字段，视为「未分类」。
   */
  code?: ErrorCode;
}

export type ChatEvent =
  | ContentDeltaEvent
  | ThinkingDeltaEvent
  | ToolCallEvent
  | ToolResultEvent
  | ClarificationEvent
  | FallbackEvent
  | UsageEvent
  | DoneEvent
  | StreamErrorEvent
  | TransportIncompleteEvent;

// =====================================================================
// 本地持久化形状（frontend/src/db/index.js）
// =====================================================================
export type ChatRole = 'user' | 'assistant';

/**
 * 一轮回复里内容块的种类，用来描述**事件到达顺序**（见各处的 `partOrder`）。
 *
 * 定义放在这里而不是 `chat-message.ts`：写入方是 `chat-store.applyEvent`（某类内容
 * 首次出现时追加自己的种类），读取方是 `chat-message.buildParts`（按它排布部件）。
 * 两边都依赖 types.ts，放在其中一边会把依赖方向弄反。
 */
export type PartKind = 'tool-call' | 'reasoning' | 'text';

export interface ToolCallRecord {
  toolCallId: string;
  toolName: string;
  argsText: string;
  result?: string;
  status: 'running' | 'complete';
}

/**
 * 挂在本轮助手消息上的待答提问。
 *
 * 字段已在服务端解析完毕（`ClarificationEvent`），前端不再解析 `argsText`；
 * 也**没有** `handled` 标记——「是否待答」由消息列表推导，见
 * `chat-message.ts:pendingClarification`。
 */
export interface ClarificationState {
  toolCallId: string;
  question: string;
  options: string[];
  allowCustomInput: boolean;
}

/**
 * 本轮回答实际由备用模型完成时记录的事实。
 *
 * 独立字段而不是拼进 `content`：notice 一旦写进正文，会被当作助手历史再次发给模型，
 * 也会被复制/导出当成回答的一部分。
 */
export interface FallbackState {
  fromModel: string;
  toModel: string;
}

/**
 * 本轮的真实用量（落库形状 `MessageRecord.usage`）。
 *
 * 独立字段而不是拼进 `content`，与 `FallbackState` 同一个理由：写进正文会被当成
 * 助手历史再次发给模型，也会被复制/导出当成回答的一部分。
 */
export interface UsageState {
  /** 本轮最后一次请求的 prompt token 数 = 当前上下文长度。 */
  contextTokens: number;
  /** 该模型的上下文窗口；网关没报时为 null（此时只展示用量，不展示占比）。 */
  contextWindow: number | null;
  /** 本轮生成的总输出 token。 */
  outputTokens: number;
  /** 上下文里命中上游提示缓存的部分（`contextTokens` 的子集）。 */
  cacheReadTokens: number;
}

/** 本轮失败的结构化记录；`retryable` 决定消息下方是否显示「重试」按钮。 */
export interface RunError {
  message: string;
  retryable: boolean;
  /** 失败分类，见 `StreamErrorEvent.code`；未分类时为 null（老记录/老服务端）。 */
  code: ErrorCode | null;
}

/**
 * 一条记录与服务端之间的同步状态。
 *
 * - `pending`：本机有还没推上去的变更。**这个状态下 pull 不许覆盖它**——否则同步会
 *   把用户刚写的内容回滚成服务端的旧版本。
 * - `synced`：服务端与本机一致。
 * - `error`：服务端明确拒绝且重试不会成功（超长、超配额）。停在 error 而不是无限重试：
 *   重试既不会成功，还会一直占用同步配额。
 *
 * 老记录没有这个字段 → 视为 `synced`（它们本来就在本机）。
 */
export type SyncState = 'pending' | 'synced' | 'error';

export interface MessageRecord {
  id: string;
  sessionId: string;
  role: ChatRole;
  content: string;
  /**
   * 本轮生成实际请求的模型 id。
   *
   * 上下文是否已满**是按模型判定的**（同一份历史在 32k 窗口上超限、在 1M 窗口上没问题），
   * 所以“这个会话被上下文锁住了”必须知道锁在哪个模型上。老记录没有这个字段 → null，
   * 按“模型未知”处理（见 `isContextExhausted`）。
   */
  model?: string | null;
  thinking?: string | null;
  thinkingMs?: number;
  toolCalls?: ToolCallRecord[];
  stopped?: boolean;
  incomplete?: boolean;
  clarification?: ClarificationState | null;
  fallback?: FallbackState | null;
  /** 本轮的真实 token 用量；上游没报时为 null（老记录同样为 null）。 */
  usage?: UsageState | null;
  error?: RunError | null;
  /**
   * 内容块到达顺序。老记录没有这个字段，`buildParts` 会退回默认顺序，
   * 所以它必须是可选的（IndexedDB 里已存在的记录不会因加字段而失效）。
   */
  partOrder?: PartKind[];
  createdAt: number;
  /** 同步状态；缺席 = 「老记录，已视为同步」。 */
  syncState?: SyncState;
  /**
   * 本机修订号：每次本地修改 +1。服务端据此拒绝「旧内容覆盖新内容」的重放。
   * **不是时间戳**，不参与任何跨设备比较。
   */
  clientRev?: number;
  /**
   * 被服务端拒收的原因码（仅 `syncState === 'error'` 时有值）。
   * 持久化的理由：刷新页面后同步状态面板仍要能解释「为什么这几条传不上去」，
   * 只在内存里留原因的话，重启后用户只看到一个没有下文的数字。
   */
  syncError?: string | null;
}

export interface SessionRecord {
  id: string;
  title: string;
  model?: string | null;
  reasoningEffort?: string;
  isPinned?: number;
  createdAt: number;
  updatedAt: number;
  /**
   * 本机墓碑：删除会话时保留一行并置 true（消息直接删掉），同步层据此把删除推给
   * 服务端与其他设备。列表查询必须过滤掉它。
   */
  deleted?: boolean;
  syncState?: SyncState;
  clientRev?: number;
  /** 见 `MessageRecord.syncError`：拒收原因码，仅 `error` 状态时有值。 */
  syncError?: string | null;
}

/**
 * 同步元数据（游标 / 回填标记）。
 *
 * 存 IndexedDB 而不是 localStorage：它必须和 sessions/messages 在同一个库、同一个
 * 「登出即清空」的生命周期里，否则换账号后会把别人的游标带过来。
 */
export interface SyncMetaRecord {
  key: string;
  /** 这份元数据属于哪个用户：换账号后旧值必须失效。 */
  userId: string;
  value: string;
  updatedAt: number;
}

// =====================================================================
// 内存里的一次生成（chat-store.ts）
// =====================================================================
export interface Run {
  sessionId: string;
  assistantMsgId: string;
  model: string | null;
  reasoningEffort: string | null;
  controller: AbortController;
  startedAt: number;
  thinking: string;
  content: string;
  /** 首次收到 thinking 增量时记录，用于结算思考耗时。 */
  thinkingStartedAt: number | null;
  thinkingMs: number;
  toolCalls: ToolCallRecord[];
  /**
   * 内容块到达顺序。`applyEvent` 追加，`chat-persistence.runFields` 原样落库；
   * 缺席时渲染退回默认顺序（老内存态 / 测试里手搓的 run）。
   */
  partOrder?: PartKind[];
  clarification: ClarificationState | null;
  fallback: FallbackState | null;
  usage: UsageState | null;
  error: RunError | null;
  sawDone: boolean;
  stopped: boolean;
  incomplete: boolean;
  finished: boolean;
}

// =====================================================================
// assistant-ui runtime 消息
// =====================================================================
/** `ThreadMessageLike['content']` 去掉字符串分支后的部件数组。 */
export type RuntimePart = Exclude<ThreadMessageLike['content'], string>[number];

export type RuntimeMessage = ThreadMessageLike;

/** 消息 `metadata.custom` 里我们自己的字段。 */
export interface MessageCustomMetadata {
  stopped: boolean;
  thinkingMs: number;
  clarification: ClarificationState | null;
  /** 本轮回答是否由备用模型完成；`thread.aui.tsx` 据此渲染一行提示。 */
  fallback?: FallbackState | null;
  /** 本轮的真实 token 用量（`MessageRecord.usage` 的运行时镜像）。 */
  usage?: UsageState | null;
  /** 本轮失败的结构化记录；可重试时消息下方渲染「重试」按钮。 */
  error?: RunError | null;
  /**
   * 本轮请求的模型 id（落库形状 `MessageRecord.model` 的运行时镜像）。
   *
   * 「会话是否被上下文锁住」靠它区分：锁只对溢出时的那个模型成立，换一个窗口
   * 更大的模型就该放行（提示文案也正是这么写的），所以这个字段不能省。
   */
  model?: string | null;
  /**
   * 本轮渲染不完整（丢过帧、或流没有干净收尾）。落库形状 `MessageRecord.incomplete`
   * 的运行时镜像：写入由 chat-message.ts 负责，展示由 thread.aui.tsx 负责。
   */
  incomplete?: boolean;
  /** 本轮终态落库失败：数据库里的内容已不可信，刷新后可能丢失。 */
  persistFailed?: boolean;
  [key: string]: unknown;
}
