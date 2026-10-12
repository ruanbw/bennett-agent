/**
 * IndexedDB 记录 / 内存 run → assistant-ui runtime 消息。
 *
 * R-5：`toRuntimeMessage` 与 `buildLiveMessage` 此前是同一段逻辑写了两遍——相同的
 * 内容数组构造、相同的「按名字过滤内部工具」、连 key 兜底 `String(Math.random())`
 * 都一样。真实差异只有 status 的计算方式与数据来源，现在合成一个 `buildParts`。
 *
 * R-6：待答提问不再是带 `handled` 标记的可变状态，而是**从消息列表推导**。见
 * `pendingClarification`。
 */

import { newId } from './constants';
import type {
  ClarificationState,
  MessageRecord,
  PartKind,
  Run,
  RuntimeMessage,
  RuntimePart,
  ToolCallRecord,
} from '../types';

// 契约本体在 types.ts（store 侧的写入方也要用），这里保留再导出，避免既有 import 路径失效
export type { PartKind };

/**
 * 旧版本把意图确认工具的调用与结果写进了 `toolCalls`；新数据不会——
 * 服务端已把它翻译成 `clarification` 事件，且不再转发该工具的结果事件。
 * 这个集合只用于清理历史记录，与前端的协议知识无关。
 */
const LEGACY_INTERNAL_TOOLS = new Set(['ask_user_clarification']);

function buildToolParts(toolCalls: ToolCallRecord[] | undefined): RuntimePart[] {
  if (!Array.isArray(toolCalls)) return [];

  return toolCalls
    .filter((call) => !LEGACY_INTERNAL_TOOLS.has(call.toolName))
    .map(
      (call): RuntimePart => ({
        type: 'tool-call',
        toolName: call.toolName || 'tool',
        toolCallId: call.toolCallId || newId('call'),
        argsText: call.argsText || '',
        result: call.result,
        // 刻意不传 status：它不在 `ThreadMessageLike` 的 tool-call 形状里，运行时也
        // 不读（见 @assistant-ui/core 的 auto-status.js），而是从 result 是否存在
        // 自行推导 running / complete。传入只会被静默忽略。
      }),
    );
}

interface BuildPartsParams {
  toolCalls?: ToolCallRecord[];
  thinking?: string | null;
  content?: string | null;
  /**
   * 事件到达顺序（见 `PartKind`）。缺失/损坏时退回默认顺序，绝不因此丢内容。
   *
   * 生产这份顺序的是 `chat-store.applyEvent`：某类内容首次出现时追加自己的种类。
   */
  order?: readonly PartKind[] | null;
  reasoningStatus: 'running' | 'complete';
  textStatus: 'running' | 'complete';
}

function isPartKind(value: unknown): value is PartKind {
  return value === 'tool-call' || value === 'reasoning' || value === 'text';
}

/**
 * 读取记录 / run 上的到达顺序字段（`partOrder`）。
 *
 * 这里刻意用鸭子类型而不是断言 `record.partOrder`：字段由 store 侧写入，老记录没有
 * 它、IndexedDB 里也可能存着被改坏的数组。校验不过就当没有——渲染的兜底是默认顺序。
 */
function readPartOrder(source: object | null | undefined): PartKind[] | undefined {
  const raw = (source as { partOrder?: unknown } | null | undefined)?.partOrder;
  if (!Array.isArray(raw)) return undefined;

  const kinds = raw.filter(isPartKind);
  return kinds.length > 0 ? kinds : undefined;
}

/**
 * 构造消息内容数组。已完成的消息与生成中的消息共用，差异只在 status。
 *
 * 顺序：有 `order` 时严格按事件到达顺序排布——按类型分组（工具→思考→正文）会把
 * 交替出现的思考与工具调用画成另一番顺序。没有 order 时退回「思考 → 工具 → 正文」：
 * 思考先于工具调用、正文在工具之后，是这套后端事件流的常态，比把工具顶到最前面
 * 更接近事实（老记录没有顺序字段，只能近似）。
 */
export function buildParts({
  toolCalls,
  thinking,
  content,
  order,
  reasoningStatus,
  textStatus,
}: BuildPartsParams): RuntimePart[] {
  const tools = buildToolParts(toolCalls);
  const reasoning: RuntimePart | null = thinking
    ? { type: 'reasoning', text: thinking, status: { type: reasoningStatus } }
    : null;
  const text: RuntimePart | null = content
    ? { type: 'text', text: content, status: { type: textStatus } }
    : null;

  const parts: RuntimePart[] = [];

  if (!order || order.length === 0) {
    if (reasoning) parts.push(reasoning);
    parts.push(...tools);
    if (text) parts.push(text);
    return parts;
  }

  let toolIndex = 0;
  let reasoningPlaced = false;
  let textPlaced = false;

  for (const kind of order) {
    if (kind === 'tool-call') {
      const tool = tools[toolIndex];
      toolIndex += 1;
      if (tool) parts.push(tool);
    } else if (kind === 'reasoning' && !reasoningPlaced) {
      reasoningPlaced = true;
      if (reasoning) parts.push(reasoning);
    } else if (kind === 'text' && !textPlaced) {
      textPlaced = true;
      if (text) parts.push(text);
    }
  }

  // 顺序表没覆盖到的块补在末尾：字段写坏或写一半也不能吞掉用户的内容
  if (!reasoningPlaced && reasoning) parts.push(reasoning);
  for (; toolIndex < tools.length; toolIndex += 1) parts.push(tools[toolIndex]);
  if (!textPlaced && text) parts.push(text);

  return parts;
}

/**
 * IndexedDB 记录 → runtime 消息（已完成的轮次）。
 */
export function toRuntimeMessage(record: MessageRecord): RuntimeMessage {
  return {
    id: record.id,
    role: record.role,
    content: buildParts({
      toolCalls: record.toolCalls,
      thinking: record.thinking,
      content: record.content,
      order: readPartOrder(record),
      reasoningStatus: 'complete',
      textStatus: 'complete',
    }),
    createdAt: new Date(record.createdAt),
    // `reason` 是 `MessageStatus` 的必填字段：只写 `{ type: 'complete' }` 会被运行时
    // 当作无效形状（旧代码就是这么写的，而 TS 从未检查过它）。
    status: record.role === 'assistant' ? { type: 'complete', reason: 'stop' } : undefined,
    metadata: {
      custom: {
        stopped: Boolean(record.stopped),
        thinkingMs: record.thinkingMs ?? 0,
        clarification: record.clarification ?? null,
        // F6 结算出的「半截回复」语义：thread 据此渲染「不完整」标记。
        // 老记录没有这个字段 → false，与修复前的呈现一致。
        incomplete: Boolean(record.incomplete),
      },
    },
  };
}

/**
 * 内存 run → runtime 消息（生成中的轮次覆盖层）。
 *
 * 内容直接取自 run，因此每次增量都会得到新的对象——这是流式渲染的前提。
 */
export function buildLiveMessage(run: Run): RuntimeMessage {
  return {
    id: run.assistantMsgId,
    role: 'assistant',
    content: buildParts({
      toolCalls: run.toolCalls,
      thinking: run.thinking,
      content: run.content,
      order: readPartOrder(run),
      // 思考结束的标志是「正文开始出现」，与主流平台一致
      reasoningStatus: run.content || run.finished ? 'complete' : 'running',
      textStatus: run.finished ? 'complete' : 'running',
    }),
    createdAt: new Date(run.startedAt),
    status: run.finished ? { type: 'complete', reason: 'stop' } : undefined,
    metadata: {
      custom: {
        stopped: run.stopped,
        thinkingMs: run.thinkingMs,
        // 与 toRuntimeMessage 同一条契约：同一轮的覆盖层与落库记录必须给出一致的
        // 「不完整」标记，否则页面刷新前后表现会不一样。
        incomplete: Boolean(run.incomplete),
        // 生成中不呈现提问：等本轮完全结束再拉起浮层，
        // 否则用户还没看清选项就可能被下一段增量挤走
        clarification: run.finished ? (run.clarification ?? null) : null,
      },
    },
  };
}

/** 待答提问 + 它挂在哪条消息上（忽略时需要这个 id）。 */
export type PendingClarification = ClarificationState & { messageId: string };

/**
 * 从消息列表推导当前待答提问（R-6）。
 *
 * 只需要看最后一条消息。提问总是挂在本轮助手消息上，于是：
 *   - 用户回答 → 追加了一条 user 消息 → 最后一条不再是 assistant → 自然消解；
 *   - 用户忽略 → 记录上的 `clarification` 被清空 → 自然消解。
 *
 * 因此不需要 `handled` 标记、不需要 `resolvedClarificationIds` 集合，也不需要
 * 「刷新页面后从数据库恢复待答状态」的循环——那三样都在补偿「状态没有推导来源」。
 */
export function pendingClarification(
  messages: readonly RuntimeMessage[] | undefined,
  isRunning: boolean,
): PendingClarification | null {
  if (isRunning) return null;

  const list = messages ?? [];
  const last = list[list.length - 1];
  if (!last || last.role !== 'assistant') return null;

  const pending = last.metadata?.custom?.clarification as ClarificationState | null | undefined;
  return pending?.question ? { messageId: last.id ?? '', ...pending } : null;
}
