/**
 * 本地记录 ⇄ 同步协议行 的映射，以及「这一行该不该覆盖本地」的判定。
 *
 * 全部是纯函数：同步里最容易出错的就是这几个判断（覆盖规则、payload 收哪些字段、
 * 会话 id 怎么对应），抽出来才能在 node 环境的 vitest 里直接钉住。
 */

import type {
  ApprovalState,
  ApprovedToolCallRecord,
  ChatRole,
  ClarificationState,
  CompactionState,
  FallbackState,
  MessageRecord,
  PartKind,
  RunError,
  SessionRecord,
  SyncState,
  ToolCallRecord,
  ToolOutcome,
  UsageState,
} from '../../types';

/** push 上行（不带服务端版本号）。字段名是线上契约（snake_case）。 */
export interface ConversationPushRow {
  id: string;
  title: string;
  model: string | null;
  reasoning_effort: string | null;
  is_pinned: boolean;
  deleted: boolean;
  client_rev: number;
  created_at: number;
  updated_at: number;
}

export interface MessagePushRow {
  id: string;
  conversation_id: string;
  role: string;
  /** 前端自有结构的整包透传；服务端不理解也不校验内部字段。 */
  payload: Record<string, unknown>;
  client_rev: number;
  created_at: number;
}

export interface ConversationRow extends ConversationPushRow {
  version: number;
}

export interface MessageRow extends MessagePushRow {
  /** 服务端赋值的会话内顺序。 */
  seq: number;
  version: number;
}

/**
 * 进 payload 的字段白名单。
 *
 * 用白名单而不是「整个记录减掉几个键」：减法的写法会在以后新增字段时默默把
 * `syncState` / `clientRev` 这类同步元数据也发上去，服务端存下来再拉回本地，
 * 就成了同步层自己污染自己。
 */
const MESSAGE_PAYLOAD_KEYS = [
  'content',
  'thinking',
  'thinkingMs',
  'toolCalls',
  'clarification',
  /*
   * 待审批的工具调用。
   *
   * 少这一项时审批轮**出不了本机**：设备 A 收到审批轮后 push 上行，payload 里没有
   * `approval`，设备 B 拉下来得到的是一条空正文、无审批的助手消息——那一轮被拦下的
   * 调用在 B 上永远无法执行（续跑要靠客户端回放 `tool_calls` 快照）。它不是显示层的
   * 装饰，而是跨设备继续这一轮的唯一凭据，与其余业务字段同级。
   */
  'approval',
  'fallback',
  'compaction',
  'usage',
  'error',
  'partOrder',
  'model',
  'stopped',
  'incomplete',
] as const;

export function toConversationPushRow(session: SessionRecord): ConversationPushRow {
  return {
    id: session.id,
    title: session.title,
    model: session.model ?? null,
    reasoning_effort: session.reasoningEffort ?? null,
    is_pinned: Boolean(session.isPinned),
    deleted: session.deleted === true,
    client_rev: session.clientRev ?? 0,
    created_at: session.createdAt,
    updated_at: session.updatedAt,
  };
}

export function toMessagePushRow(record: MessageRecord): MessagePushRow {
  const payload: Record<string, unknown> = {};
  for (const key of MESSAGE_PAYLOAD_KEYS) {
    if (record[key] !== undefined) payload[key] = record[key];
  }
  return {
    id: record.id,
    conversation_id: record.sessionId,
    role: record.role,
    payload,
    client_rev: record.clientRev ?? 0,
    created_at: record.createdAt,
  };
}

/** 服务端行 → 本地会话。`local` 存在时保留它上面同步层不认识的字段。 */
export function fromConversationRow(row: ConversationRow, local?: SessionRecord): SessionRecord {
  return {
    ...local,
    id: row.id,
    title: row.title,
    model: row.model,
    reasoningEffort: row.reasoning_effort ?? undefined,
    isPinned: row.is_pinned ? 1 : 0,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
    deleted: row.deleted,
    syncState: 'synced',
    clientRev: row.client_rev,
  };
}

const TOOL_OUTCOMES: readonly ToolOutcome[] = ['success', 'failed', 'denied', 'interrupted'];
const PART_KINDS: readonly PartKind[] = ['tool-call', 'reasoning', 'text'];

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : null;
}

function asFiniteNumber(value: unknown): number | undefined {
  return typeof value === 'number' && Number.isFinite(value) ? value : undefined;
}

function sanitizeToolCalls(value: unknown): ToolCallRecord[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const calls: ToolCallRecord[] = [];
  for (const item of value) {
    const raw = asRecord(item);
    if (!raw) continue;
    const toolCallId = typeof raw.toolCallId === 'string' ? raw.toolCallId : undefined;
    const toolName = typeof raw.toolName === 'string' ? raw.toolName : undefined;
    const argsText = typeof raw.argsText === 'string' ? raw.argsText : undefined;
    const status = raw.status === 'running' || raw.status === 'complete' ? raw.status : undefined;
    if (!toolCallId || !toolName || argsText === undefined || !status) continue;

    const call: ToolCallRecord = { toolCallId, toolName, argsText, status };
    if (typeof raw.result === 'string') call.result = raw.result;
    if (TOOL_OUTCOMES.includes(raw.outcome as ToolOutcome)) call.outcome = raw.outcome as ToolOutcome;
    const startedAt = asFiniteNumber(raw.startedAt);
    const finishedAt = asFiniteNumber(raw.finishedAt);
    if (startedAt !== undefined) call.startedAt = startedAt;
    if (finishedAt !== undefined) call.finishedAt = finishedAt;
    calls.push(call);
  }
  return calls;
}

function sanitizeClarification(value: unknown): ClarificationState | undefined {
  const raw = asRecord(value);
  if (!raw) return undefined;
  const toolCallId = typeof raw.toolCallId === 'string' ? raw.toolCallId : undefined;
  const question = typeof raw.question === 'string' ? raw.question : undefined;
  const allowCustomInput = typeof raw.allowCustomInput === 'boolean' ? raw.allowCustomInput : undefined;
  const options = Array.isArray(raw.options)
    ? raw.options.filter((option): option is string => typeof option === 'string')
    : undefined;
  if (toolCallId === undefined || question === undefined || allowCustomInput === undefined || !options) {
    return undefined;
  }
  return { toolCallId, question, options, allowCustomInput };
}

/**
 * 待审批调用的形状校验。
 *
 * 与 `sanitizeToolCalls` 刻意不同：那里「坏的那条丢掉、好的留下」，这里**只要有一条
 * 不合法就整块丢掉**。理由是这个结构唯一用途是**逐条表态**——少一条调用，用户就会在
 * 一个不完整的清单上按「提交决定」，提交出去的决定集合与那一轮真实被拦下的调用对不上，
 * 而卡片本身的文案（「以下工具调用尚未执行」）会让用户以为自己看到了全部。半个审批框
 * 比没有审批框更危险：没有框至少还能靠报错发现异常。
 *
 * 一条都没有同样整块丢掉：`toolCalls: []` 会渲染出一张「0 条待确认」、无法提交、
 * 也无法忽略的卡片。
 *
 * 返回 `undefined` 表示「这一行没有可用的审批数据」，调用方据此**不动**本地字段；
 * 而显式的 `null` 是另一回事，见 `sanitizeMessagePayload` 里对它的处理。
 */
function sanitizeApproval(value: unknown): ApprovalState | undefined {
  const raw = asRecord(value);
  if (!raw) return undefined;
  if (!Array.isArray(raw.toolCalls) || raw.toolCalls.length === 0) return undefined;

  const toolCalls: ApprovedToolCallRecord[] = [];
  for (const item of raw.toolCalls) {
    const call = asRecord(item);
    if (!call) return undefined;
    const toolCallId = typeof call.toolCallId === 'string' && call.toolCallId ? call.toolCallId : undefined;
    const toolName = typeof call.toolName === 'string' && call.toolName ? call.toolName : undefined;
    const argsText = typeof call.argsText === 'string' ? call.argsText : undefined;
    // description 缺失不致命（退化成空串会让卡片少一行，但决定仍可做出）；
    // riskLevel 缺失同样退化成空串，由 `describeRisk` 走「未知风险、按最高风险呈现」，
    // 这是安全的一侧，不需要因此丢掉整块。
    const description = typeof call.description === 'string' ? call.description : '';
    const riskLevel = typeof call.riskLevel === 'string' ? call.riskLevel : '';
    // 前三个是提交决定的必需键：`toolCallId` 是决定的 key，缺了就没法表态；
    // `toolName` / `argsText` 是用户判断后果的唯一依据，缺了就是让用户盲签。
    if (toolCallId === undefined || toolName === undefined || argsText === undefined) return undefined;
    toolCalls.push({ toolCallId, toolName, argsText, description, riskLevel });
  }
  return { toolCalls };
}

function sanitizeFallback(value: unknown): FallbackState | undefined {
  const raw = asRecord(value);
  if (!raw) return undefined;
  const fromModel = typeof raw.fromModel === 'string' ? raw.fromModel : undefined;
  const toModel = typeof raw.toModel === 'string' ? raw.toModel : undefined;
  if (fromModel === undefined || toModel === undefined) return undefined;
  const reason = typeof raw.reason === 'string' ? raw.reason : raw.reason === null ? null : undefined;
  return reason === undefined ? { fromModel, toModel } : { fromModel, toModel, reason };
}

/**
 * 压缩记录的形状校验。
 *
 * `strategy` 必须是两个已知取值之一：它是**渲染分支的判据**（截断 / 折叠两套文案），
 * 协议外的字符串会让显示层落到空白分支——用户看到一行没有任何文字的图标。
 * 四个数字缺失时整条丢掉：一个「压缩了 0 条消息」的提示比不显示更让人困惑。
 */
function sanitizeCompaction(value: unknown): CompactionState | undefined {
  const raw = asRecord(value);
  if (!raw) return undefined;
  const strategy = raw.strategy === 'truncate' || raw.strategy === 'summarize' ? raw.strategy : undefined;
  if (strategy === undefined) return undefined;
  const usedTokensBefore = asFiniteNumber(raw.usedTokensBefore);
  const usedTokensAfter = asFiniteNumber(raw.usedTokensAfter);
  const limitTokens = asFiniteNumber(raw.limitTokens);
  if (usedTokensBefore === undefined || usedTokensAfter === undefined || limitTokens === undefined) {
    return undefined;
  }
  return {
    strategy,
    droppedMessages: asFiniteNumber(raw.droppedMessages) ?? 0,
    truncatedMessages: asFiniteNumber(raw.truncatedMessages) ?? 0,
    usedTokensBefore,
    usedTokensAfter,
    limitTokens,
  };
}

function sanitizeUsage(value: unknown): UsageState | undefined {
  const raw = asRecord(value);
  if (!raw) return undefined;
  const contextTokens = asFiniteNumber(raw.contextTokens);
  const outputTokens = asFiniteNumber(raw.outputTokens);
  const cacheReadTokens = asFiniteNumber(raw.cacheReadTokens);
  if (contextTokens === undefined || outputTokens === undefined || cacheReadTokens === undefined) return undefined;
  const nullableNumber = (input: unknown): number | null =>
    typeof input === 'number' && Number.isFinite(input) ? input : null;
  return {
    contextTokens,
    contextWindow: nullableNumber(raw.contextWindow),
    contextLimitTokens: nullableNumber(raw.contextLimitTokens),
    outputTokens,
    cacheReadTokens,
  };
}

function sanitizeError(value: unknown): RunError | undefined {
  const raw = asRecord(value);
  if (!raw) return undefined;
  const message = typeof raw.message === 'string' ? raw.message : undefined;
  const retryable = typeof raw.retryable === 'boolean' ? raw.retryable : undefined;
  if (message === undefined || retryable === undefined) return undefined;
  const error: RunError = {
    message,
    retryable,
    code: typeof raw.code === 'string' ? (raw.code as RunError['code']) : null,
  };
  if (typeof raw.retryAfterAt === 'number' && Number.isFinite(raw.retryAfterAt)) {
    error.retryAfterAt = raw.retryAfterAt;
  } else if (raw.retryAfterAt === null) {
    error.retryAfterAt = null;
  }
  return error;
}

/**
 * 把服务端下发的 `payload` 收敛成本地认识的结构。
 *
 * 服务端只保证 payload 是一个 JSON object：换设备/被攻陷的后端/旧客户端都可能塞进
 * 形状不对的字段。此前 `...payload` 直接铺进 `MessageRecord`，一个
 * `thinking: {a: 1}` 或 `clarification.question: {...}` 就会让 React 在渲染
 * 「对象不能作为子节点」时抛错、整个会话白屏（L-08）。
 *
 * 这里逐字段做类型守卫：坏字段丢弃（或回退到安全默认值），未知字段一律不进来。
 */
export function sanitizeMessagePayload(value: unknown): Partial<MessageRecord> {
  const raw = asRecord(value);
  if (!raw) return {};

  const sanitized: Partial<MessageRecord> = {};
  if (typeof raw.content === 'string') sanitized.content = raw.content;
  if (typeof raw.thinking === 'string' || raw.thinking === null) sanitized.thinking = raw.thinking;
  if (typeof raw.thinkingMs === 'number' && Number.isFinite(raw.thinkingMs)) {
    sanitized.thinkingMs = raw.thinkingMs;
  }
  if (typeof raw.stopped === 'boolean') sanitized.stopped = raw.stopped;
  if (typeof raw.incomplete === 'boolean') sanitized.incomplete = raw.incomplete;
  if (typeof raw.model === 'string' || raw.model === null) sanitized.model = raw.model;

  const toolCalls = sanitizeToolCalls(raw.toolCalls);
  if (toolCalls !== undefined) sanitized.toolCalls = toolCalls;
  const clarification = sanitizeClarification(raw.clarification);
  if (clarification !== undefined) sanitized.clarification = clarification;
  const approval = sanitizeApproval(raw.approval);
  if (approval !== undefined) sanitized.approval = approval;
  /*
   * 显式 `null` = 另一台设备**忽略了**这次审批，必须照写下去。
   *
   * 这是唯一一个「缺席」与「null」语义相反的字段，值得单独一段说明：
   *
   * - 字段缺席（老记录 / 服务端没带）→ 不动本地的 `approval`，因为 `fromMessageRow`
   *   是 `{...local, ...payload}`，写 undefined 会把本地值抹掉、而且区分不出「服务端
   *   没说」与「服务端说没有」；
   * - 显式 `null` → **写下去**。它是清除信号：设备 B 忽略审批后推
   *   `approval: null` 上行，设备 A 拉到这一行时若不落这个 null，本地那条
   *   `{toolCalls: [...]}` 就会因为对象展开而存活下来——卡片在 A 上永远删不掉，
   *   再点「忽略」也只是重复同一次无效的本地清理。
   *
   * 不写 `sanitizeApproval` 里返回 null：那个函数的返回值约定是
   * 「有可用结构 / 没有可用结构」，把 null 混进去会让「坏数据」和「清除指令」
   * 在调用方看来一模一样。
   */
  if (raw.approval === null) sanitized.approval = null;
  const fallback = sanitizeFallback(raw.fallback);
  if (fallback !== undefined) sanitized.fallback = fallback;
  const compaction = sanitizeCompaction(raw.compaction);
  if (compaction !== undefined) sanitized.compaction = compaction;
  const usage = sanitizeUsage(raw.usage);
  if (usage !== undefined) sanitized.usage = usage;
  const error = sanitizeError(raw.error);
  if (error !== undefined) sanitized.error = error;
  if (Array.isArray(raw.partOrder)) {
    sanitized.partOrder = raw.partOrder.filter((kind): kind is PartKind =>
      PART_KINDS.includes(kind as PartKind),
    );
  }
  return sanitized;
}

/** 服务端行 → 本地消息。 */
export function fromMessageRow(row: MessageRow, local?: MessageRecord): MessageRecord {
  const payload = sanitizeMessagePayload(row.payload);
  return {
    ...local,
    ...payload,
    id: row.id,
    sessionId: row.conversation_id,
    role: row.role as ChatRole,
    // content 是必填字段：服务端允许它为空串（只产出思考链的历史、流式占位），
    // 但绝不允许缺席——缺了就是坏数据，补空串比留 undefined 更安全。
    content: typeof payload.content === 'string' ? payload.content : '',
    createdAt: row.created_at,
    syncState: 'synced',
    clientRev: row.client_rev,
  };
}

/**
 * 拉到的这一行是否应该覆盖本地。
 *
 * 三条规则，顺序不能换：
 *
 * 1. 本地没有 → 直接建；
 * 2. 本地是 `pending` / `error` → **不动**。pending 意味着本机有还没推上去的内容，
 *    拿服务端的旧版本盖掉它就是同步把用户刚写的消息回滚；error 是服务端明确拒绝过
 *    的行，覆盖会让它看起来像成功；
 * 3. 其余按 `client_rev` 比较：同一行的修订号只有写它的那台设备会推进，所以
 *    「服务端更大」= 那台设备后来又改了它。
 *
 * 不用 `version` 比较：它是全用户级别的游标，和「这一行被改过几次」无关。
 */
export function shouldApplyRemote(
  row: { client_rev: number },
  local?: { syncState?: SyncState; clientRev?: number },
): boolean {
  if (!local) return true;
  if (local.syncState === 'pending' || local.syncState === 'error') return false;
  return row.client_rev > (local.clientRev ?? 0);
}

export interface PushBatch {
  conversations: ConversationPushRow[];
  messages: MessagePushRow[];
}

/**
 * 组一批要推送的行。
 *
 * 两条约束：
 *
 * 1. 会话必须先于它的消息到达服务端（服务端会按 id 校验会话归属）；
 * 2. 会话数超出一批时，**不能**把引用这些会话的消息塞进同一批——那只会换回一堆
 *    `conversation_missing`。本轮先推会话，消息下一批再说。
 */
export function buildPushBatch(
  sessions: readonly SessionRecord[],
  messages: readonly MessageRecord[],
  limit: number,
): PushBatch {
  // 同毫秒的会话按 id 兜底排序：批次的顺序必须确定，否则「谁先到服务端」随
  // 引擎实现细节漂移，排查问题时无法复现。
  const orderedSessions = [...sessions].sort(
    (a, b) => a.createdAt - b.createdAt || a.id.localeCompare(b.id),
  );
  const batchSessions = orderedSessions.slice(0, limit);
  const batchedIds = new Set(batchSessions.map((session) => session.id));
  const pendingIds = new Set(orderedSessions.map((session) => session.id));

  const eligible = messages.filter(
    (message) => !pendingIds.has(message.sessionId) || batchedIds.has(message.sessionId),
  );
  // 会话内按时间排：服务端按到达顺序赋 seq，顺序稳定才能让两端看到同样的消息次序
  const orderedMessages = [...eligible].sort(
    (a, b) =>
      a.sessionId.localeCompare(b.sessionId) || a.createdAt - b.createdAt || a.id.localeCompare(b.id),
  );

  return {
    conversations: batchSessions.map(toConversationPushRow),
    messages: orderedMessages
      .slice(0, Math.max(limit - batchSessions.length, 0))
      .map(toMessagePushRow),
  };
}
