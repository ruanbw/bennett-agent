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

export interface DoneEvent {
  type: 'done';
}

export interface StreamErrorEvent {
  type: 'error';
  /** 只含可以外泄的文案（校验原文或错误编号），见 CODE_REVIEW.md C-4。 */
  message: string;
}

export type ChatEvent =
  | ContentDeltaEvent
  | ThinkingDeltaEvent
  | ToolCallEvent
  | ToolResultEvent
  | ClarificationEvent
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

export interface MessageRecord {
  id: string;
  sessionId: string;
  role: ChatRole;
  content: string;
  thinking?: string | null;
  thinkingMs?: number;
  toolCalls?: ToolCallRecord[];
  stopped?: boolean;
  incomplete?: boolean;
  clarification?: ClarificationState | null;
  /**
   * 内容块到达顺序。老记录没有这个字段，`buildParts` 会退回默认顺序，
   * 所以它必须是可选的（IndexedDB 里已存在的记录不会因加字段而失效）。
   */
  partOrder?: PartKind[];
  createdAt: number;
}

export interface SessionRecord {
  id: string;
  title: string;
  model?: string | null;
  reasoningEffort?: string;
  isPinned?: number;
  createdAt: number;
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
  /**
   * 本轮渲染不完整（丢过帧、或流没有干净收尾）。落库形状 `MessageRecord.incomplete`
   * 的运行时镜像：写入由 chat-message.ts 负责，展示由 thread.aui.tsx 负责。
   */
  incomplete?: boolean;
  /** 本轮终态落库失败：数据库里的内容已不可信，刷新后可能丢失。 */
  persistFailed?: boolean;
  [key: string]: unknown;
}
