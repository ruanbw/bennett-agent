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
  Run,
  RuntimeMessage,
  RuntimePart,
  ToolCallRecord,
} from '../types';

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
  reasoningStatus: 'running' | 'complete';
  textStatus: 'running' | 'complete';
}

/**
 * 构造消息内容数组。已完成的消息与生成中的消息共用，差异只在 status。
 */
export function buildParts({
  toolCalls,
  thinking,
  content,
  reasoningStatus,
  textStatus,
}: BuildPartsParams): RuntimePart[] {
  const parts = buildToolParts(toolCalls);

  if (thinking) {
    parts.push({ type: 'reasoning', text: thinking, status: { type: reasoningStatus } });
  }
  if (content) {
    parts.push({ type: 'text', text: content, status: { type: textStatus } });
  }

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
