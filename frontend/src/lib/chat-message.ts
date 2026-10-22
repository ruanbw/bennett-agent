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
import { toolResultForDisplay } from './tool-call-view';
import type {
  ApprovalState,
  ClarificationState,
  MessageCustomMetadata,
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

/** 把落库的起止时间映射成运行时要求的 `timing`；缺起点时返回 undefined（不显示耗时）。 */
function toolCallTiming(call: ToolCallRecord): { startedAt: number; completedAt?: number } | undefined {
  if (typeof call.startedAt !== 'number') return undefined;
  return {
    startedAt: call.startedAt,
    completedAt: typeof call.finishedAt === 'number' ? call.finishedAt : undefined,
  };
}

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
        // 失败结果包一层带 outcome 的对象：运行时只看 result 是否存在，直接丢字符串
        // 会把隔离后的工具异常画成绿色成功。
        result: toolResultForDisplay(call.outcome, call.result),
        // 刻意不传 status：它不在 `ThreadMessageLike` 的 tool-call 形状里，运行时也
        // 不读（见 @assistant-ui/core 的 auto-status.js），而是从 result 是否存在
        // 自行推导 running / complete。传入只会被静默忽略。
        //
        // timing 则**必须**显式传：`useToolCallElapsed`（耗时 chip）读的就是部件上的
        // `timing.completedAt - timing.startedAt`，而运行时不会替我们补。此前没有它，
        // ToolFallbackDuration 任何时候都拿到 undefined，那个 chip 其实是死代码。
        // 老记录没有这两个时间戳 → 不传 timing，行为与之前一致（不显示耗时）。
        timing: toolCallTiming(call),
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
        // 待审批调用：刷新后审批卡片要能重新出现，否则那一轮被拦下的调用永久卡死
        approval: record.approval ?? null,
        // 备用模型信息：老记录没有该字段 → null，呈现与修复前一致
        fallback: record.fallback ?? null,
        // 压缩信息：老记录没有该字段 → null，不显示提示行
        compaction: record.compaction ?? null,
        // 真实用量：刷新后「上下文用了多少」仍然可见
        usage: record.usage ?? null,
        // 失败信息：刷新后「重试」按钮仍按服务端当初的判定显示
        error: record.error ?? null,
        // 本轮用的模型：刷新后「上下文已满」的锁仍然只对那个模型成立
        model: record.model ?? null,
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
        // 仅生成中的消息有开始时间；组件重挂载（切会话再回来）靠它续计时，
        // 不能用 thinkingMs（内容出现前一直是 0）。
        ...(run.thinkingStartedAt !== null ? { thinkingStartedAt: run.thinkingStartedAt } : {}),
        // 与 toRuntimeMessage 同一条契约：同一轮的覆盖层与落库记录必须给出一致的
        // 「不完整」标记，否则页面刷新前后表现会不一样。
        incomplete: Boolean(run.incomplete),
        // 与落库记录保持一致：切换提示在生成中就显示，不必等本轮结束
        fallback: run.fallback ?? null,
        // 压缩提示同理：它发生在生成之前，用户在第一帧就该看到
        compaction: run.compaction ?? null,
        // 真实用量在本轮收尾前到达（done 之前），覆盖层与落库记录给出同一个数字
        usage: run.usage ?? null,
        // 失败信息：错误到达后立即映射，生成结束时按钮已可点
        error: run.error ?? null,
        // 与落库记录同一条契约：锁在哪个模型上，覆盖层与刷新后必须给出同一个答案
        model: run.model,
        // 生成中不呈现提问：等本轮完全结束再拉起浮层，
        // 否则用户还没看清选项就可能被下一段增量挤走
        clarification: run.finished ? (run.clarification ?? null) : null,
        // 审批同理：`approval_required` 是本轮最后一条事件，所以它到达时本轮已经
        // 没有后续增量了；仍按 finished 收口，避免卡片闪一下又被顶掉。
        approval: run.finished ? (run.approval ?? null) : null,
      },
    },
  };
}

/** 待答提问 + 它挂在哪条消息上（忽略时需要这个 id）。 */
export type PendingClarification = ClarificationState & { messageId: string };

/** 待审批调用 + 它挂在哪条消息上（续跑时需要这个 id 与那个 msgId）。 */
export type PendingApproval = ApprovalState & { messageId: string };

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

/**
 * 从消息列表推导当前待审批的工具调用。
 *
 * 与 `pendingClarification` 同一条原则：**只推导，不存标记**。
 *   - 用户批准/拒绝 → 记录上的 `approval` 被清空 → 自然消解；
 *   - 用户不发请求就关掉页面 → 记录还在 → 下次打开审批框仍然出现。
 *
 * `isRunning` 时返回 null：审批请求到达意味着本轮已经结束，但生成态下不该抢先拉
 * 起卡片（那时 `done` 可能还没到，用户看到卡片却点不动）。
 */
export function pendingApproval(
  messages: readonly RuntimeMessage[] | undefined,
  isRunning: boolean,
): PendingApproval | null {
  if (isRunning) return null;

  const list = messages ?? [];
  const last = list[list.length - 1];
  if (!last || last.role !== 'assistant') return null;

  const pending = last.metadata?.custom?.approval as ApprovalState | null | undefined;
  if (!pending?.toolCalls?.length) return null;
  return { messageId: last.id ?? '', ...pending };
}

/**
 * 本会话是否已被上下文上限锁住——即「禁止继续提问」的推导来源。
 *
 * 只需要看最后一条助手消息，理由与 `pendingClarification` 完全相同：
 *
 *   - 换到新会话 → 消息列表里没有这条记录 → 自然解锁；
 *   - 换了模型 → 下面按模型比对的规则让它自然解锁（提示文案也说了可以换更大的模型）。
 *
 * 与「服务端每轮都会拒绝」的关系：服务端是真正的闸门（它按模型的 context_window
 * 判定），这里只是**别让用户白点一次发送**——历史只会更长，同一模型下重发必然同样
 * 失败，而他那时才看到报错。所以这里是 UX 层的推导，不是第二份判定逻辑：它只认
 * 服务端给出的 `error.code`，自己不算任何 token。
 *
 * 锁只在溢出时的那个模型上成立：同一份历史在 32k 窗口上超限、在 1M 窗口上没问题。
 * 拿不到模型 id（老记录）时不再区分模型，一律锁——宁可多拦一次，也不要让用户
 * 对着一个必然失败的会话反复点发送。
 */
export function contextExhausted(
  messages: readonly RuntimeMessage[] | undefined,
  currentModel: string | null,
): boolean {
  const list = messages ?? [];
  const last = list[list.length - 1];
  if (!last || last.role !== 'assistant') return false;

  const custom = last.metadata?.custom as MessageCustomMetadata | undefined;
  if (custom?.error?.code !== 'context_overflow') return false;

  const lockedOn = custom.model;
  return typeof lockedOn !== 'string' || lockedOn === currentModel;
}
