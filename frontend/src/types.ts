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
// GET /api/capabilities（app/schemas/capabilities.py）
// =====================================================================
/**
 * 当前服务端**实际装配**的能力快照。
 *
 * 前端用它裁剪空对话页的起步建议：没有联网搜索时不要再推荐「搜索最新动态」。
 * 不能按模型列表或默认配置猜——占位 API Key 在配置上非空，工具却必然失败。
 */
export interface Capabilities {
  web_search: boolean;
  tools: string[];
  /**
   * 已配置**且连通**的 MCP server。连不上的不在这个列表里——它们的原因在 `mcp_error`。
   *
   * 可选：老服务端不返回这个字段，此时按「没有 MCP」处理。
   */
  mcp_servers?: McpServerStatus[];
  /**
   * MCP 配了但没连上的原因；全部连通或未配置时为 null。
   *
   * 单独给出这个字段是为了让「配了但不通」可被界面解释：只让工具从列表里消失，
   * 用户（和运维）会以为配置从来没生效。
   */
  mcp_error?: string | null;
}

// =====================================================================
// 知识库（RAG）
// =====================================================================
/**
 * 知识库是否可用。
 *
 * `enabled=false` 意味着**服务端没配 EMBEDDING_MODEL**，不是用户没上传过东西：
 * 前端必须先看它再决定是否显示上传表单，否则用户填完标题点提交才看到一句
 * 「知识库未启用」。
 */
export interface KnowledgeCapabilities {
  enabled: boolean;
  /** 当前使用的 embedding 模型（未启用时为 null）。 */
  embeddingModel: string | null;
  /** 不可用的原因与改法（不是错误，是部署状态）。 */
  reason: string | null;
}

export interface KnowledgeDocument {
  id: string;
  title: string;
  /** 来源说明（文件名 / URL）；没填时为 null。 */
  source: string | null;
  /** 这篇文档被切成了多少块。 */
  chunkCount: number;
  /** 生成向量时用的模型。换模型后必须重建，否则维度对不上。 */
  embeddingModel: string;
  dimensions: number;
  createdAt: string;
}

export interface KnowledgeDocumentList {
  documents: KnowledgeDocument[];
  /** 文档数上限与当前用量：前端据此提示「还能再传几篇」。 */
  maxDocuments: number;
  totalChunks: number;
}

/** 手动检索的一条结果。两路分数分开给出，便于判断「靠关键词还是靠语义命中的」。 */
export interface KnowledgeSearchHit {
  documentId: string;
  documentTitle: string;
  heading: string;
  text: string;
  score: number;
  vectorScore: number;
  lexicalScore: number;
}

/** 一个 MCP server 的运行时状态。`name` 同时也是它的工具名前缀。 */
export interface McpServerStatus {
  name: string;
  /** 该 server 提供、且已装配的工具名（不含前缀）。 */
  tools: string[];
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

/** 工具执行结果；`success` 以外的值不应被渲染成绿色成功。 */
export type ToolOutcome = 'success' | 'failed' | 'denied' | 'interrupted';

export interface ToolResultEvent {
  type: 'tool_result';
  tool_call_id: string;
  tool_name: string;
  result: string;
  /** 旧服务端不返回该字段，视为 success（与修复前一致）。 */
  outcome?: ToolOutcome;
}

export interface ClarificationEvent {
  type: 'clarification';
  tool_call_id: string;
  question: string;
  options: string[];
  allow_custom_input: boolean;
}

/**
 * 一条待审批的工具调用。
 *
 * 服务端把工具在**执行前**拦下来（见后端 `app/tools/approval.py`），所以收到这个
 * 事件时那些调用一次都没有执行。
 */
export interface ApprovalToolCall {
  tool_call_id: string;
  tool_name: string;
  /** 调用入参，JSON 文本；展示层决定怎么格式化。 */
  args: string;
  /** 工具说明，帮用户理解「这是要做什么」。 */
  description: string;
  /** 风险等级（`read_only` / `safe_write` / `dangerous` …）。 */
  risk_level: string;
}

/**
 * 本轮有工具调用需要用户批准，尚未执行。
 *
 * 审批是**分段**的：服务端无状态，没法在本轮内暂停等待，所以本轮就此结束，用户
 * 做出决定后再发起一次请求续跑。客户端必须把 `tool_calls` 持久化到这条助手消息上
 * ——续跑时服务端要靠它认出「上一次到底要调用什么」。
 */
export interface ApprovalRequiredEvent {
  type: 'approval_required';
  tool_calls: ApprovalToolCall[];
}

export interface FallbackEvent {
  type: 'fallback';
  /** 失败的主模型 id（网关路由格式）。 */
  from_model: string;
  /** 实际完成本轮回答的备用模型 id。 */
  to_model: string;
  /** 切换原因类别（「上游限流」「上游 503」…）；老服务端不带时为空串。 */
  reason?: string;
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
  /**
   * 后端判定超限时使用的**真实 prompt** 上限（窗口扣除回答预留、再乘安全系数）。
   *
   * 与 `context_tokens` 同口径（都含系统提示词与工具 schema），所以两者相除才是
   * 「上下文用了多少」。`context_window` 是原始窗口，拿它当分母会严重低估占用。
   * 可选：旧服务端 / 预算守卫关闭 / 备用模型接手时没有这个数，前端退回窗口口径。
   */
  context_limit_tokens?: number | null;
  output_tokens: number;
  cache_read_tokens: number;
  /**
   * 本轮估算成本。三者都缺失表示**当前模型没有配置价格**——不是「免费」。
   *
   * 后端按「真正回答的那个模型」的单价算（备用模型接手的轮次不能按主模型单价算），
   * 并在没有价格时整体缺席。前端据此显示「未配置价格」而不是 `$0.00`：把 0 展示给
   * 用户会被读成「免费」，那是一个明确的错误结论。
   */
  cost_total?: number | null;
  cost_currency?: string | null;
  /** 缓存命中省下的钱（相对「全部按普通输入价」）。用于说明缓存到底有没有起作用。 */
  cost_cache_savings?: number | null;
}

/**
 * 失败分类。
 *
 * - `context_overflow`：上下文超出模型窗口。重试必然同样失败（历史只会更长），
 *   用户只能新建会话或换窗口更大的模型——所以既不给「重试」按钮，也不能只说「出错了」。
 * - `model_unavailable`：数据库配置里这个模型已停用或不存在。刷新模型列表就能看到真实
 *   清单，重试同一个模型同样失败。
 * - `internal_error`：服务端自己出错了（查库失败、模板渲染失败等），**没有**关于模型的
 *   确定结论；已附关联 ID，可重试。
 *
 * 展示口径（标题 / 下一步建议）在 `lib/run-error.ts`，认不出的取值一律当未分类。
 */
export type ErrorCode = 'context_overflow' | 'model_unavailable' | 'internal_error';

/**
 * 服务端在**本轮开始前**做过上下文压缩。
 *
 * 压缩改了模型看到的上下文（折叠了早期轮次，或截断了超长消息），所以必须显式
 * 告诉用户：不说的话，用户会把「模型忘了刚才说的」当成模型变笨。
 * 与 `FallbackEvent` 是同一条原则——凡是改变了本轮回答依据的事，都要说出来。
 *
 * 摘要正文**不在**这个事件里：它走服务端的运行期 instructions，不下发是为了不把
 * 早期对话的敏感内容落进浏览器并同步到其他设备。
 */
export interface CompactionEvent {
  type: 'compaction';
  /** `truncate` = 只截断了超长消息；`summarize` = 折叠了早期轮次。 */
  strategy: 'truncate' | 'summarize';
  /** 被折叠成摘要的消息条数（`truncate` 时为 0）。 */
  dropped_messages?: number;
  /** 被截断了中间部分的消息条数。 */
  truncated_messages?: number;
  used_tokens_before: number;
  used_tokens_after: number;
  limit_tokens: number;
}

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
  | ApprovalRequiredEvent
  | FallbackEvent
  | CompactionEvent
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
  /** 服务端给的执行结果；老记录没有该字段 → success。 */
  outcome?: ToolOutcome;
  /**
   * 本机收到 `tool_call` / `tool_result` 事件的时间戳（毫秒）。
   *
   * 只有落库的这两个时间戳能让耗时在**刷新之后**仍然显示：assistant-ui 的
   * `useToolCallElapsed` 读的是消息部件的 `timing`，而部件的 timing 由这里的记录
   * 映射而来（见 `chat-message.buildToolParts`）。老记录没有它们 → 不显示耗时。
   */
  startedAt?: number;
  finishedAt?: number;
}

/**
 * 挂在本轮助手消息上的**待审批工具调用**。
 *
 * 与 `ClarificationState` 同一个套路：字段已在服务端解析完毕，前端不再解析
 * `argsText` 之外的东西。「是否待批」由消息列表推导（见 `chat-message.ts`），
 * 不存 `handled` 标记——存了就会有「标记与真实状态不一致」的第二种可能。
 */
export interface ApprovedToolCallRecord {
  toolCallId: string;
  toolName: string;
  /** 调用入参，JSON 文本（原样展示）。 */
  argsText: string;
  description: string;
  riskLevel: string;
}

export interface ApprovalState {
  toolCalls: ApprovedToolCallRecord[];
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
  /**
   * 切换原因的类别短语（「上游限流」「上游 503」「请求超时」…）。
   *
   * 老服务端 / 老记录没有这个字段：此时只显示「已切换」，不编造原因。
   */
  reason?: string | null;
}

/**
 * 本轮开始前做过的上下文压缩（落库形状 `MessageRecord.compaction`）。
 *
 * 与 `FallbackState` 同一个理由独立成字段：写进正文会被当成助手历史再次发给模型，
 * 也会被复制/导出当成回答的一部分。落库是必需的——刷新页面后用户仍要能看到
 * 「这一轮之前压缩过」，否则他会以为模型忽然开始忘事。
 */
export interface CompactionState {
  strategy: 'truncate' | 'summarize';
  /** 被折叠成摘要的消息条数（`truncate` 时为 0）。 */
  droppedMessages: number;
  /** 被截断了中间部分的消息条数。 */
  truncatedMessages: number;
  usedTokensBefore: number;
  usedTokensAfter: number;
  limitTokens: number;
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
  /**
   * 后端判定超限时使用的真实 prompt 上限（窗口扣除回答预留后乘安全系数）。
   *
   * 与 `contextTokens` 同口径，进度条与剩余量都以它为准；缺失时退回 `contextWindow`。
   */
  contextLimitTokens: number | null;
  /** 本轮生成的总输出 token。 */
  outputTokens: number;
  /** 上下文里命中上游提示缓存的部分（`contextTokens` 的子集）。 */
  cacheReadTokens: number;
  /**
   * 本轮估算成本（按当前登记的模型价格换算）。
   *
   * `null` = 该模型没有配置价格，**不是免费**。界面必须显示「未配置价格」而不是
   * `$0.00`——后者会被读成「这个模型不花钱」，而实际可能只是没人填价格。
   *
   * 这里刻意只存金额本身，不存单价：单价会随上游调价与管理员改配置而变化，把它快照
   * 进历史消息里会让旧记录显示一个「当时的」单价，与账目口径冲突。会话累计金额由
   * 各轮金额相加得到，那才是稳定的。
   */
  costTotal?: number | null;
  costCurrency?: string | null;
  costCacheSavings?: number | null;
}

/** 本轮失败的结构化记录；`retryable` 决定消息下方是否显示「重试」按钮。 */
export interface RunError {
  message: string;
  retryable: boolean;
  /** 失败分类，见 `StreamErrorEvent.code`；未分类时为 null（老记录/老服务端）。 */
  code: ErrorCode | null;
  /**
   * 服务端要求等到这个时刻（epoch ms）再重试；没有 `Retry-After` 时为 null/缺失。
   *
   * 存**绝对时刻**而不是「还有几秒」：这份错误会随消息落库，刷新后若把相对秒数当剩余
   * 时间，倒计时会从零再走一遍，而实际上早就该能重试了。
   */
  retryAfterAt?: number | null;
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
  /**
   * 本轮被审批拦下的工具调用；缺失=没有待审批（老记录同样缺失）。
   *
   * **必须落库**：续跑时服务端要靠客户端回放的 `tool_calls` 才知道「上一次要调用
   * 什么」。刷新页面后这份数据还在，审批框才能重新出现并继续工作。
   */
  approval?: ApprovalState | null;
  fallback?: FallbackState | null;
  /**
   * 本轮开始前做过的上下文压缩；没压缩时为 null/缺失（老记录同样缺失）。
   *
   * 落库的理由：刷新后用户仍要能看到「这一轮之前压缩过」，否则他会以为模型忽然
   * 开始忘事，而现场没有任何痕迹能解释这件事。
   */
  compaction?: CompactionState | null;
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
  /** 本轮开始前做过的上下文压缩；没压缩时为 null。 */
  compaction: CompactionState | null;
  /**
   * 内容块到达顺序。`applyEvent` 追加，`chat-persistence.runFields` 原样落库；
   * 缺席时渲染退回默认顺序（老内存态 / 测试里手搓的 run）。
   */
  partOrder?: PartKind[];
  clarification: ClarificationState | null;
  /** 本轮被审批拦下的调用；没有需要审批的调用时为 null。 */
  approval: ApprovalState | null;
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
  /**
   * 本轮思考开始的时间戳（仅内存中的生成中消息有）。
   *
   * 切到别的会话再回来时 ReasoningView 会重挂载；只靠 thinkingMs（内容出现前为 0）
   * 会从 0 重新计时。落库记录没有这个字段，历史消息按已结算的 thinkingMs 展示。
   */
  thinkingStartedAt?: number | null;
  clarification: ClarificationState | null;
  /** 本轮被审批拦下的调用（运行时镜像）；`thread.aui.tsx` 据此渲染审批卡片。 */
  approval?: ApprovalState | null;
  /** 本轮回答是否由备用模型完成；`thread.aui.tsx` 据此渲染一行提示。 */
  fallback?: FallbackState | null;
  /** 本轮开始前的上下文压缩；`thread.aui.tsx` 据此渲染一行提示。 */
  compaction?: CompactionState | null;
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
