/**
 * 运行过程的持久化（R-4 拆分之三）。
 *
 * 变化理由：存储结构。只认识 `db` 与 run 快照，不认识协议、不认识 UI。
 *
 * 已知限制：多标签页打开同一会话时没有跨页互斥，两边各自落库、后写的覆盖先写的
 * （IndexedDB 不提供跨页事务协调；详见 chat-store.ts 顶部说明）。
 */

import { db } from '../db';
import { requestPush } from './sync/engine';
import { isRunIncomplete, type RunState } from './chat-store';
import { stripNotices } from './message-copy';
import type { ClarificationState, MessageRecord, Run } from '../types';

type MessageChanges = Partial<Omit<MessageRecord, 'id' | 'sessionId'>>;

/** run 里需要落库的字段。 */
function runFields(run: Run): MessageChanges {
  return {
    content: run.content,
    thinking: run.thinking,
    thinkingMs: run.thinkingMs,
    toolCalls: run.toolCalls,
    // 本轮请求的模型：上下文是否已满按模型判定，锁定状态靠它区分
    model: run.model ?? null,
    // 事件到达顺序：没有它，刷新后思考/工具/正文只能按默认顺序近似还原
    partOrder: run.partOrder ?? [],
    // 备用模型信息：刷新后提示行仍然要显示，否则用户会以为还是主模型答的
    fallback: run.fallback ?? null,
    // 压缩信息：刷新后仍要能看到「这一轮之前压缩过」，否则模型忽然忘事无从解释
    compaction: run.compaction ?? null,
    // 真实用量：刷新后「上下文用了多少」依旧可见（它也是估算器下一轮的锚点）
    usage: run.usage ?? null,
    // 失败信息：刷新后「重试」按钮仍然可用（是否可重试由服务端判定并持久化）
    error: run.error ?? null,
    // 待审批的调用：**必须落库**，因为续跑时服务端要靠客户端回放的 `tool_calls`
    // 才知道「上一次到底要调用什么」。不落库的话刷新一次审批框就永久消失，
    // 那一轮被拦下的调用再也无法执行。
    approval: run.approval ?? null,
  };
}

/**
 * 流式过程中的节流落库：刷新或关页面时不至于丢掉整段回复。
 *
 * 落库的内容一定标 `incomplete: true`：这是「半截」的持久化，页面刷新后必须能
 * 看出它没跑完。终态落库（saveFinal）会按真实终止原因把它改回准确值。
 */
export async function saveProgress(assistantMsgId: string, run: Run): Promise<void> {
  await db.messages.update(assistantMsgId, { ...runFields(run), incomplete: true });
}

/**
 * 终态落库。
 *
 * `stopped` / `incomplete` 都取 run 上的结算值（与 finishRun 共用 `isRunIncomplete`）：
 * 只有真的不完整才写 true；干净结束必须把 saveProgress 写下的 `incomplete: true`
 * 覆盖回 false，否则刷新后一条正常回复也会被标成「可能不完整」。
 */
export async function saveFinal(assistantMsgId: string, run: RunState): Promise<void> {
  await db.messages.update(assistantMsgId, {
    ...runFields(run),
    stopped: run.stopped,
    incomplete: isRunIncomplete(run),
    clarification: run.clarification ?? null,
  });
  // 一轮结束是推送的自然时机：中间态（saveProgress）只打标记，不上传。
  // 引擎没在登录态时是空操作，所以纯逻辑测试不受影响。
  requestPush();
}

export async function touchSession(sessionId: string): Promise<void> {
  await db.sessions.update(sessionId, { updatedAt: Date.now() });
}

/**
 * 清掉助手消息上的待答提问。
 *
 * 这就是「用户忽略了提问」的全部语义：记录上不再带 `clarification`，
 * 于是 `pendingClarification` 的推导条件自然不成立。不需要额外的 `handled` 标记。
 */
export async function clearMessageClarification(messageId: string): Promise<void> {
  await db.messages.update(messageId, { clarification: null });
}

/**
 * 清掉助手消息上的待审批调用。
 *
 * 用户做出决定（或决定忽略）之后调用：记录上不再带 `approval`，
 * `pendingApproval` 的推导条件自然不成立，审批卡片随之消失。
 *
 * 与 `clearMessageClarification` 分开而不是合成一个「清待答」：两者的数据形状与
 * 消失时机不同——提问是「用户答了」，审批是「用户批了或拒了」，而后者还要带着
 * 决定去续跑。
 */
export async function clearMessageApproval(messageId: string): Promise<void> {
  await db.messages.update(messageId, { approval: null });
  // 与 `saveFinal` 末尾同一个理由：这一行得推上去。
  //
  // 表级 hook 已经把本行标成 pending、修订号 +1（`db/index.ts`），所以这里只需要
  // 叫醒推送引擎。少了这一步，`approval: null` 永远留在本机：别的设备下次 pull
  // 拿到的还是带 `approval` 的旧行，点过「忽略」的卡片会被重新拉回来——用户看到
  // 「我明明忽略过了」。这是 M3 的跨设备那一面。
  requestPush();
}

export async function loadSessionMessages(sessionId: string): Promise<MessageRecord[]> {
  return db.messages.where('sessionId').equals(sessionId).sortBy('createdAt');
}

export async function countSessionMessages(sessionId: string): Promise<number> {
  return db.messages.where('sessionId').equals(sessionId).count();
}

// ---------------------------------------------------------------------
// 上下文回放
// ---------------------------------------------------------------------
/**
 * 回放的工具调用（**线上形状，snake_case**）。
 *
 * 只有续跑审批时才会带上：服务端要靠它认出「上一次到底要调用什么」。
 * 字段名跟 `context_anchor` 一样按服务端契约写 snake_case，因为这个对象会被
 * 原样塞进请求体，中间没有一层映射。
 */
export interface ContextToolCall {
  tool_call_id: string;
  tool_name: string;
  args: string;
}

/** 送往模型的历史消息。与 `chat-transport` 的请求形状一致：role、content，可选 tool_calls。 */
export interface ContextMessage {
  role: string;
  content: string;
  /** 仅在续跑审批时出现；其余情况不带（多带会白白放大每轮请求体）。 */
  tool_calls?: ContextToolCall[];
}

/**
 * 回传的真实用量锚点：`messageIndex` 指的是**送达模型的那个数组**里的位置。
 *
 * 下标必须对应 `messages` 而不是本地记录：中间还可能被 `clampContextMessages` 裁过、
 * 空白占位消息也被滤过，位置早就不是记录里的了。
 */
export interface ContextAnchor {
  messageIndex: number;
  contextTokens: number;
}

/**
 * 回放用的消息：比协议多一个「这条消息产生的那轮真实用量」。
 *
 * 它只在本地推导锚点时有用，发请求前会被剥掉（`loadContextMessages` 的返回把消息
 * 压回 `{role, content}`）——协议里多一个字段就多一处两边要对齐的地方。
 */
interface ReplayableMessage extends ContextMessage {
  anchorTokens?: number;
}

/**
 * 单条 content 的前端上限。后端是 32_000（`app/schemas/chat.py:MAX_MESSAGE_CHARS`），
 * 这里留 2000 字符余量：同一份内容在两侧的计数口径将来可能微调，贴边等于随时 422。
 */
export const CONTEXT_MAX_MESSAGE_CHARS = 30_000;

/**
 * 历史总量的前端上限。后端是 400_000（`MAX_HISTORY_CHARS`，按 content+thinking 计），
 * 这里同样留余量。前端不回放 thinking，所以实际统计的就是各条 content 之和。
 */
export const CONTEXT_MAX_TOTAL_CHARS = 380_000;

export interface ClampContextOptions {
  maxContentChars?: number;
  maxTotalChars?: number;
}

function totalChars(messages: readonly ContextMessage[]): number {
  let total = 0;
  for (const message of messages) total += message.content.length;
  return total;
}

/**
 * 把历史压进后端的硬上限，避免「一条超长回复让该会话每次请求都 422」的永久卡死。
 *
 * 策略固定为「先截单条、再丢最旧、必要时截断最长」：
 *   1. 单条超上限就截断——超长的那一条只要还在，每一轮都会被后端拒掉；
 *   2. 总量超上限就丢最旧——越旧的消息对当前轮次越不重要；
 *   3. 只剩一条仍超总量（单条上限比总预算还大）时才截断最长的一条。
 *
 * 纯函数：不改入参，未超限时原样返回内容（不做任何无谓改写）。
 */
export function clampContextMessages<T extends ContextMessage>(
  messages: readonly T[],
  {
    maxContentChars = CONTEXT_MAX_MESSAGE_CHARS,
    maxTotalChars = CONTEXT_MAX_TOTAL_CHARS,
  }: ClampContextOptions = {},
): T[] {
  const withinPerMessage = messages.every((message) => message.content.length <= maxContentChars);
  if (withinPerMessage && totalChars(messages) <= maxTotalChars) return [...messages];

  // `as T`：这里只改 content，其余字段（含锚点这类本地附加字段）原样带过去。
  // 泛型无法表达「浅拷贝并覆盖某字段仍是 T」，但这是本函数唯一的改动方式。
  const clamped = messages.map((message) =>
    (message.content.length > maxContentChars
      ? { ...message, content: message.content.slice(0, maxContentChars) }
      : { ...message }) as T,
  );

  while (clamped.length > 1 && totalChars(clamped) > maxTotalChars) clamped.shift();

  const total = totalChars(clamped);
  if (total > maxTotalChars && clamped.length > 0) {
    let longest = 0;
    for (let i = 1; i < clamped.length; i += 1) {
      if (clamped[i].content.length > clamped[longest].content.length) longest = i;
    }
    const others = total - clamped[longest].content.length;
    const budget = Math.max(0, maxTotalChars - others);
    clamped[longest] = { ...clamped[longest], content: clamped[longest].content.slice(0, budget) } as T;
  }

  return clamped;
}

/**
 * 把带 clarification 的助手轮合成为可回放的文本。
 *
 * 提问只存在消息的 `clarification` 字段里，`content` 往往是空的。此前整条被
 * 「content 为空的助手消息」过滤掉，用户的回答就成了孤立 user 消息——模型看不到
 * 自己刚问的问题，答非所问。
 */
function renderClarification(clarification: ClarificationState): string {
  const lines = [clarification.question];
  if (clarification.options.length > 0) {
    lines.push('', '可选项：');
    clarification.options.forEach((option, index) => lines.push(`${index + 1}. ${option}`));
  }
  lines.push('', '（用户下一条消息是对该问题的回答。）');
  return lines.join('\n');
}

/**
 * 正文为空的助手轮合成为占位文本。
 *
 * 改动前这里是「滤掉」：正文为空的助手消息直接从回放里消失，于是它两边的一问一答
 * 变成相邻的两条 user 消息，服务端按「必须严格交替」拒掉（`app/schemas/chat.py:
 * _check_message_order`），而且是**永久** 422——历史只在增长，下一轮同样非法，
 * 用户没有任何出路。两个不需要任何故障就能走到的场景：
 *
 *   1. 发送后首 token 之前按 Esc 停止 → `saveFinal` 落库 `content: ''`；
 *   2. 工具审批轮只发起调用、正文为空 → 用户点 × 忽略（只清 `approval`，正文仍空）；
 *      或者干脆不理会，直接提下一个问题。
 *
 * 改成合成文本后，形状自然恢复成 [user, assistant, user]：占位同时在两侧合法
 * （后端只要求 assistant 的 content 非空白语义无关，user 才要求非空白）。
 * 文案要写成**第三人称的事实陈述**：这段话会被当作助手历史发给模型，用第一人称
 * 会让模型以为「我刚才说了这句」。
 */
function renderEmptyAssistant(message: MessageRecord): string {
  // 待审批优先：那一轮的操作还没有发生，说「已停止」会误导模型以为用户放弃了
  if (message.approval?.toolCalls?.length) return '（本轮工具调用待审批，未执行）';
  if (message.stopped) return '（本轮已停止，未产生回答）';
  if (message.error) return '（本轮请求失败，未产生回答）';
  // 只跑了工具、没有正文：确实是「没回答」，但也别让模型以为用户没提过问题
  if (message.toolCalls?.length) return '（本轮只执行了工具调用，未产生正文回答）';
  return '（本轮未产生回答）';
}

function toReplayable(message: MessageRecord): ReplayableMessage {
  // 先剥掉尾部系统提示：失败/断流/跨标签冲突时 `chat-store` 会往正文尾部追加
  // `> ⚠️ **请求失败**: …`。那段话是给用户看的，回放给模型就成了「上一轮我自己用
  // 人类口吻写的一句告警」——断 3 次历史里就有 3 段。剥完才判空：一条只有提示、
  // 没有正文的失败轮次应当走占位文本，而不是把提示当成回答发上去。
  const body = message.role === 'assistant' ? stripNotices(message.content) : message.content;

  const content =
    message.role === 'assistant' && !body.trim()
      ? message.clarification
        ? renderClarification(message.clarification)
        : renderEmptyAssistant(message)
      : body;
  // `body` 与 `message.content` 的差异只在尾部提示：正文为空但提示非空时，上面的
  // 分支已经把它换成占位文本，不会再原样发出提示

  // 只有助手消息能带用量：它是那一轮回答产生的那次请求的 prompt token 数
  const anchorTokens =
    message.role === 'assistant' && typeof message.usage?.contextTokens === 'number'
      ? message.usage.contextTokens
      : undefined;

  return { role: message.role, content, anchorTokens };
}

/**
 * 合并两条同角色消息的正文。空的一方不留下多余的空行。
 */
function joinContents(left: string, right: string): string {
  const a = left.trim();
  const b = right.trim();
  if (!a) return b;
  if (!b) return a;
  return `${a}\n\n${b}`;
}

/**
 * 把回放历史修成后端接受的形状：**首条 user、严格交替、末条 user**。
 *
 * 这是本文件里唯一一处「形状合法性」的保证，两步裁剪（条数上限、字符上限）都必须
 * 经过它——两步各自都会把首条切成 assistant：
 *
 *   - `slice(-limit)` 按条数切，而一次正常的历史是「成对问答 + 末尾新提问」，长度恒为
 *     **奇数**（2N+1）；`HISTORY_LIMIT = 30` 是偶数，所以切点必然落在 assistant 上。
 *     31 条切 30 条后首条就是 assistant，`POST /api/chat` 直接 422。
 *   - `clampContextMessages` 丢最旧同样从头部吃消息，吃掉的条数是任意值。
 *
 * 另外两件事也在这一处收口：正文空白的 user 消息后端必拒（`_require_user_content`），
 * 以及理论上可能出现的同角色相邻（服务端合并/丢弃会让模型看到的上下文与用户以为的
 * 不一致）。
 *
 * 纯函数：不改入参，返回新数组。
 */
export function shapeContextMessages(
  messages: readonly ReplayableMessage[],
): ReplayableMessage[] {
  const out: ReplayableMessage[] = [];

  for (const message of messages) {
    // 空白 user 消息一定会被后端拒（422），留着就是给这个会话埋一颗永久地雷
    if (message.role === 'user' && !message.content.trim()) continue;

    const last = out[out.length - 1];
    if (last && last.role === message.role) {
      // 同角色相邻：合成一条。刻意不用「丢掉旧的」——那会静默吞掉一整轮内容，
      // 而模型看不到的东西无法在界面上被用户察觉。
      //
      // 用量锚点取**较晚**那条：`anchorTokens` 是「截止到这一条（含）的上下文 token
      // 数」，合并后这一格吃掉了两条的内容，用早那条的数会**低估**真实上下文，
      // 服务端的预检于是放行一次必然超限的请求。晚那条覆盖的范围才包含合并进来的内容。
      out[out.length - 1] = {
        role: message.role,
        content: joinContents(last.content, message.content),
        anchorTokens: message.anchorTokens ?? last.anchorTokens,
        // 目前 `toReplayable` 不产出 `tool_calls`（只有审批续跑的调用方会在形状合法
        // 之后单独追加一条），这里保留是为了让合并本身不吞数据——将来若有人从回放里
        // 带上工具调用，丢掉的是一条无结果的调用，服务端会按非法历史拒掉。
        ...(last.tool_calls || message.tool_calls
          ? { tool_calls: [...(last.tool_calls ?? []), ...(message.tool_calls ?? [])] }
          : {}),
      };
      continue;
    }

    out.push(message);
  }

  // 首条必须是 user：切点落在 assistant 上时往后推，推到最近的 user 为止
  while (out.length > 0 && out[0].role !== 'user') out.shift();

  // 末条必须是 user。审批续跑是唯一的例外，由调用方在形状合法的历史之后自己追加
  // 那条带 `tool_calls` 的助手消息（见 `chat-runs.ts:executeRun`）。
  while (out.length > 0 && out[out.length - 1].role !== 'user') out.pop();

  return out;
}

/**
 * 读取送往模型的历史上下文。
 *
 * 四点刻意为之：
 * 1. 排除本轮占位消息。正文为空的助手消息**不丢**——它们被合成占位文本，见
 *    `renderEmptyAssistant`；只有确实无法回放的（没有记录的）才消失。
 * 2. **不回放 `thinking`**。思考链只用于展示：把它回传会白白放大每次请求的体积，
 *    而且长度不可控——一旦超过后端的 `MAX_THINKING_CHARS`，该会话的每一次重试都会
 *    拿到 422，用户被永久卡住。
 * 3. 形状先过 `shapeContextMessages`，再统一过 `clampContextMessages`：条数裁剪挡不住
 *    单条超长与总量超长。
 * 4. 字符裁剪之后**再对齐一次形状**：`clampContextMessages` 丢最旧时同样会把首条切成
 *    assistant，不重对齐就等于没修。
 */
export async function loadContextMessages(
  sessionId: string,
  { excludeId, limit }: { excludeId: string; limit: number },
): Promise<{ messages: ContextMessage[]; anchor: ContextAnchor | null }> {
  const all = await loadSessionMessages(sessionId);

  const replayable = all
    .filter((message) => message.id !== excludeId)
    .slice(-limit)
    .map(toReplayable);

  const shaped = shapeContextMessages(replayable);
  const clamped = clampContextMessages(shaped);
  // 裁剪可能又吃掉开头的几条：形状必须在**最终数组**上成立，否则前面两步都白做
  const finalMessages = shapeContextMessages(clamped);

  // 锚点在**裁剪之后**算：下标要对应真正发出去的那个数组。
  // 从这里往回找最后一条带真实用量的助手消息——它的 `contextTokens` 覆盖了它之前的
  // 全部内容（含系统提示词），所以预检只需再估算它之后的那几条。
  let anchor: ContextAnchor | null = null;
  for (let index = finalMessages.length - 1; index >= 0; index -= 1) {
    const tokens = finalMessages[index].anchorTokens;
    if (typeof tokens === 'number' && tokens > 0) {
      anchor = { messageIndex: index, contextTokens: tokens };
      break;
    }
  }

  // 只把协议字段交出去：`anchorTokens` 是本地推导用的，不跟着发上游
  return {
    messages: finalMessages.map(({ role, content, tool_calls }) => ({
      role,
      content,
      ...(tool_calls ? { tool_calls } : {}),
    })),
    anchor,
  };
}
