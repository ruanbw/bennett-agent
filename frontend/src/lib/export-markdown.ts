/**
 * 会话导出：本地记录 → Markdown 文本 + 一个下载动作。
 *
 * 为什么值得有：会话只存在浏览器 IndexedDB 里（服务端同步只是补强），用户换浏览器、
 * 清缓存、或者只是想在没有客户端的场合引用一段回答时，没有任何出口。复制单条回答
 * 解决不了「整段对话」这个需求。
 *
 * ## 导出内容的边界（故意写死，不跟 UI 的折叠状态走）
 *
 * - **正文**：用户消息、助手正文、待答提问（问题 + 选项）。
 * - **本轮事实**：停止生成 / 回复不完整 / 切了备用模型 / 本轮失败。这些状态在界面上
 *   是回答下方的小字，导出时不带上就等于把一段失败的回复伪装成正常回答。
 * - **工具调用**：名称、参数、结果，各自截断到 `MAX_TOOL_TEXT_CHARS`。参数与结果可能
 *   是几十 KB 的 JSON，全量导出会让文件难以阅读；截断处明确标注原文长度。
 * - **不导出思考链**：界面上默认折叠、体积不可控，且它是模型的过程草稿而不是回答内容。
 *
 * Markdown 是本模块唯一的输出格式：它是纯文本、可 diff、任何编辑器都能看。
 */

import type { MessageRecord, SessionRecord, ToolCallRecord } from '../types';
import { formatTokens } from './context-usage';
import { formatToolDuration } from './tool-call-view';

/** 工具参数/结果各自的截断上限（字符）。 */
export const MAX_TOOL_TEXT_CHARS = 1000;
/** 文件名主干长度上限：留出 `.md` 与不同文件系统的余量。 */
export const MAX_FILENAME_CHARS = 60;

export interface ExportConversationOptions {
  /** 导出时刻（毫秒）。测试注固定值，生产用当前时间。 */
  exportedAt?: number;
}

export interface ExportedConversation {
  filename: string;
  markdown: string;
  /**
   * 归档里实际写入的会话数。
   *
   * 批量导出会跳过墓碑会话（用户已经删掉的那些），所以「选中 6 个」不等于「导出 6 个」。
   * 提示文案必须用这个数字，否则删完会话再导出会看到对不上的计数。
   */
  sessionCount?: number;
}

function pad2(value: number): string {
  return String(value).padStart(2, '0');
}

/** `YYYY-MM-DD HH:mm`（本地时区）：导出文件是给人看的，不写 ISO/UTC。 */
export function formatTimestamp(ms: number): string {
  const date = new Date(ms);
  if (Number.isNaN(date.getTime())) return '时间未知';
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ${pad2(
    date.getHours(),
  )}:${pad2(date.getMinutes())}`;
}

/** `YYYYMMDD-HHmm`：文件名里可排序、无空格。 */
function timestampSlug(ms: number): string {
  const date = new Date(ms);
  return `${date.getFullYear()}${pad2(date.getMonth() + 1)}${pad2(date.getDate())}-${pad2(
    date.getHours(),
  )}${pad2(date.getMinutes())}`;
}

/**
 * 从标题生成下载文件名。
 *
 * 去掉路径分隔符与 Windows 保留字符（否则保存时会被系统替换成奇怪的东西），折叠空白，
 * 截到 `MAX_FILENAME_CHARS`；标题为空时退回带时间戳的默认名，避免所有导出都叫 `md`。
 */
export function exportFileName(title: string, now: number = Date.now()): string {
  const cleaned = (title || '')
    // 控制字符（用 Unicode 分类而不是字面量区间：字面量会触发 no-control-regex）
    .replace(/\p{Cc}/gu, ' ')
    // 路径分隔符与 Windows 不允许的字符
    .replace(/[\\/:*?"<>|]/g, '-')
    .replace(/\s+/g, ' ')
    .trim()
    // 首尾的点与空格（Windows 会静默丢弃结尾的点）
    .replace(/^[. ]+|[. ]+$/g, '');

  // 按**码点**截断：`slice` 走 UTF-16，会在代理对中间切断 emoji，得到一个非法字符
  // （文件名里出现 U+FFFD 或半个字符），而界面上同一套口径已经统一用码点。
  const base = Array.from(cleaned).slice(0, MAX_FILENAME_CHARS).join('').trim();
  return `${base || `对话-${timestampSlug(now)}`}.md`;
}

/**
 * 用比正文里最长反引号串更长的围栏包裹代码块。
 *
 * 工具结果里经常本身就带 ```（尤其模型生成的 Markdown 示例）：固定三反引号会让导出文件
 * 从那里开始错位。CommonMark 允许更长的围栏，所以按最长游程 +1 取。
 */
export function fencedBlock(text: string, language = ''): string {
  const longest = Math.max(0, ...(text.match(/`+/g) ?? []).map((run) => run.length));
  const fence = '`'.repeat(Math.max(3, longest + 1));
  return `${fence}${language}\n${text}\n${fence}`;
}

function truncate(text: string, limit: number): string {
  const chars = Array.from(text);
  // 同样按码点计数与切片：工具参数/结果里出现 emoji 很常见，切断代理对会写出半个字符
  if (chars.length <= limit) return text;
  return `${chars.slice(0, limit).join('')}\n…（已截断，原文 ${chars.length} 字符）`;
}

/**
 * 一条记录是否值得出现在导出文件里。
 *
 * 除了正文与提问，**失败 / 备用模型 / 停止**也要留：一条没有正文的失败轮次如果不导出，
 * 文件里就会出现「用户问了、然后什么都没有」的空洞，读者无法判断是模型没回答还是
 * 导出把它吃掉了。
 */
function hasRenderableContent(message: MessageRecord): boolean {
  return Boolean(
    message.content.trim() ||
      message.clarification ||
      message.error ||
      message.fallback ||
      message.stopped ||
      message.incomplete ||
      (message.toolCalls && message.toolCalls.length > 0),
  );
}

function renderClarification(question: string, options: readonly string[]): string[] {
  const lines = [`> **提问：** ${question}`];
  if (options.length > 0) {
    lines.push('>');
    options.forEach((option, index) => lines.push(`> ${index + 1}. ${option}`));
  }
  return lines;
}

/** 本轮发生过的、会影响「这段回答能不能当真」的事实。 */
function renderNotices(message: MessageRecord): string[] {
  const notices: string[] = [];
  if (message.stopped) notices.push('> ⏹ 生成被用户停止，此回答不完整。');
  else if (message.incomplete) notices.push('> ⚠ 回复不完整（连接中断或生成异常）。');
  if (message.fallback) {
    notices.push(`> ⇄ 本轮由备用模型完成：${message.fallback.fromModel} → ${message.fallback.toModel}`);
  }
  if (message.error) notices.push(`> ✕ 本轮失败：${message.error.message}`);
  return notices;
}

/**
 * 一次工具调用的耗时文案；任一端缺失时返回空串。
 *
 * 只认落库的两个时间戳（`startedAt` / `finishedAt`）：还在跑的调用没有完成时间，
 * 算出来的「到现在为止」每次导出都不一样，那种数不该写进文件。
 */
function toolCallDuration(toolCall: ToolCallRecord): string {
  const { startedAt, finishedAt } = toolCall;
  if (typeof startedAt !== 'number' || typeof finishedAt !== 'number') return '';
  const elapsed = finishedAt - startedAt;
  if (!Number.isFinite(elapsed) || elapsed < 0) return '';
  return formatToolDuration(elapsed);
}

/**
 * 本轮真实用量的那一行；上游没报用量时返回空数组。
 *
 * 界面上的用量只保留**最后一轮**（进度条答的是「现在还剩多少」），导出则是给人翻的
 * 存档：每一轮消耗了多少 token、缓存命中了多少，事后才说得清「这次为什么这么贵」。
 * 数字口径与界面完全一致（`contextTokens` 是最后一次请求的 prompt，不是整轮累加）。
 */
function renderUsage(message: MessageRecord): string[] {
  const usage = message.usage;
  if (!usage || !Number.isFinite(usage.contextTokens) || usage.contextTokens <= 0) return [];

  const parts = [
    `上下文 ${formatTokens(usage.contextTokens)} tokens`,
    `输出 ${formatTokens(usage.outputTokens ?? 0)} tokens`,
  ];
  if (usage.cacheReadTokens > 0) parts.push(`缓存命中 ${formatTokens(usage.cacheReadTokens)}`);
  return [`> 用量：${parts.join(' · ')}`];
}

function renderToolCall(toolCall: ToolCallRecord): string[] {
  const outcomeLabel =
    toolCall.outcome === 'failed'
      ? '失败'
      : toolCall.outcome === 'denied'
        ? '被拒绝'
        : toolCall.outcome === 'interrupted'
          ? '已中断'
          : '完成';
  // 「已完成但没有结果」= 停在半途（用户点停止 / 连接断开 / 上游中途报错）：
  // 既不能写「完成」（没有结果可以佐证），也不该写「进行中」（它永远不会再返回了）。
  const interrupted = toolCall.status !== 'running' && toolCall.outcome === undefined && !toolCall.result;
  const status = toolCall.status === 'running' ? '进行中' : interrupted ? '未完成' : outcomeLabel;
  // 耗时与界面上折叠条右侧的「2.2s」同一口径（`formatToolDuration`）：导出文件里没有
  // 这个数，事后就无法判断一步工具调用是「秒回」还是「卡了一分钟」。
  const duration = toolCallDuration(toolCall);
  const summary = `工具调用 · ${toolCall.toolName} · ${status}${duration ? ` · ${duration}` : ''}`;
  const lines = ['<details>', `<summary>${summary}</summary>`, ''];

  const args = toolCall.argsText?.trim();
  if (args) {
    lines.push('**参数**', '', fencedBlock(truncate(args, MAX_TOOL_TEXT_CHARS), 'json'), '');
  }
  const result = toolCall.result?.trim();
  if (result) {
    lines.push('**结果**', '', fencedBlock(truncate(result, MAX_TOOL_TEXT_CHARS), 'text'), '');
  }

  lines.push('</details>');
  return lines;
}

/** 单个会话的渲染参数（内部；对外只有两个构建函数）。 */
interface RenderConversationOptions {
  /** 标题层级：单会话文件用 1，归档里每个会话用 2（角色标题固定深一级）。 */
  titleLevel?: number;
  /**
   * 元信息形状。
   * - `full`：品牌 + 创建/更新时间 + 消息数（单会话文件）
   * - `compact`：一行「最后更新 + 消息数」（归档里每个会话一段，重复品牌信息只是噪音）
   */
  meta?: 'full' | 'compact';
  exportedAt?: number;
}

function renderConversation(
  session: Pick<SessionRecord, 'title' | 'createdAt' | 'updatedAt'>,
  messages: readonly MessageRecord[],
  { titleLevel = 1, meta = 'full', exportedAt = Date.now() }: RenderConversationOptions = {},
): string[] {
  const renderable = messages.filter(hasRenderableContent);
  const title = session.title?.trim() || '未命名对话';
  const heading = (level: number): string => '#'.repeat(level);

  const lines: string[] = [`${heading(titleLevel)} ${title}`, ''];

  if (meta === 'full') {
    lines.push(
      '> 导出自 Bennett Agent',
      `> 创建时间：${formatTimestamp(session.createdAt)}`,
      `> 最后更新：${formatTimestamp(session.updatedAt)}`,
      `> 导出时间：${formatTimestamp(exportedAt)}`,
      `> 消息数：${renderable.length}`,
      '',
      '---',
      '',
    );
  } else {
    lines.push(
      `> 最后更新：${formatTimestamp(session.updatedAt)} · 消息数：${renderable.length}`,
      '',
    );
  }

  if (renderable.length === 0) {
    lines.push('（这条会话还没有可导出的消息。）', '');
    return lines;
  }

  for (const message of renderable) {
    const role = message.role === 'user' ? '你' : '助手';
    lines.push(`${heading(titleLevel + 1)} ${role} · ${formatTimestamp(message.createdAt)}`, '');

    const content = message.content.trim();
    if (content) lines.push(content, '');

    if (message.clarification) {
      lines.push(...renderClarification(message.clarification.question, message.clarification.options), '');
    }

    const notices = renderNotices(message);
    if (notices.length > 0) lines.push(...notices, '');

    for (const toolCall of message.toolCalls ?? []) {
      lines.push(...renderToolCall(toolCall), '');
    }

    const usage = renderUsage(message);
    if (usage.length > 0) lines.push(...usage, '');

    lines.push('---', '');
  }

  return lines;
}

/**
 * 生成整段会话的 Markdown（单会话文件）。
 *
 * 纯函数：不读库、不碰 DOM、不依赖当前时间（除非调用方不传 `exportedAt`），因此可以直接
 * 用固定时间戳做快照断言。消息顺序就是传入顺序（调用方按 `createdAt` 排序）。
 */
export function buildConversationMarkdown(
  session: Pick<SessionRecord, 'title' | 'createdAt' | 'updatedAt'>,
  messages: readonly MessageRecord[],
  { exportedAt = Date.now() }: ExportConversationOptions = {},
): string {
  return renderConversation(session, messages, { exportedAt }).join('\n');
}

/** 归档里的一段会话。 */
export interface ArchiveEntry {
  session: Pick<SessionRecord, 'title' | 'createdAt' | 'updatedAt'>;
  messages: readonly MessageRecord[];
}

/** 归档文件名：`Bennett-对话导出-YYYYMMDD-HHmm.md`（可排序、无空格）。 */
export function archiveFileName(now: number = Date.now()): string {
  return `Bennett-对话导出-${timestampSlug(now)}.md`;
}

/**
 * 把多个会话合并成一个 Markdown 归档（批量导出的「备份」形态）。
 *
 * 不逐个触发下载：浏览器会拦截「连续多个下载」并要求用户确认，选中 20 个会话就是
 * 20 次确认。一个文件更接近用户真正想要的「备份」，也方便一次性搜索/归档。
 *
 * 每段会话的标题降为 `##`，正文标题跟着降一级；会话之间用 `---` 分隔。归档头除了会话数
 * 还写总消息数与时间范围——判断「这份备份是否覆盖我要的那段时间」只看会话数不够。
 */
export function buildArchiveMarkdown(
  entries: readonly ArchiveEntry[],
  { exportedAt = Date.now() }: ExportConversationOptions = {},
): string {
  const messageCount = entries.reduce(
    (sum, entry) => sum + entry.messages.filter(hasRenderableContent).length,
    0,
  );
  // 时间范围取会话的创建/更新时间：消息可能为空（空会话也要能看出它属于哪段时间）
  const stamps = entries.flatMap((entry) => [entry.session.createdAt, entry.session.updatedAt]);
  const validStamps = stamps.filter((value) => Number.isFinite(value) && value > 0);
  const rangeStart = validStamps.length > 0 ? Math.min(...validStamps) : null;
  const rangeEnd = validStamps.length > 0 ? Math.max(...validStamps) : null;

  const lines: string[] = [
    '# Bennett 对话导出',
    '',
    '> 导出自 Bennett Agent',
    `> 导出时间：${formatTimestamp(exportedAt)}`,
    `> 会话数：${entries.length}`,
    `> 消息数：${messageCount}`,
  ];
  // 只有「输出」是可加的：`contextTokens` 是每次请求的 prompt 规模，累加没有意义。
  const outputTokens = entries.reduce(
    (sum, entry) => sum + entry.messages.reduce((inner, message) => inner + (message.usage?.outputTokens ?? 0), 0),
    0,
  );
  if (outputTokens > 0) lines.push(`> 输出 token 合计：${formatTokens(outputTokens)}`);
  if (rangeStart !== null && rangeEnd !== null) {
    lines.push(`> 时间范围：${formatTimestamp(rangeStart)} → ${formatTimestamp(rangeEnd)}`);
  }
  lines.push('', '---', '');

  if (entries.length === 0) {
    lines.push('（没有可导出的会话。）', '');
    return lines.join('\n');
  }

  for (const entry of entries) {
    lines.push(
      ...renderConversation(entry.session, entry.messages, {
        titleLevel: 2,
        meta: 'compact',
        exportedAt,
      }),
    );
    // 有消息的会话已经以 `---` 收尾；空会话要补一条，否则两段会粘在一起
    if (!entry.messages.some(hasRenderableContent)) lines.push('---', '');
  }

  return lines.join('\n');
}

/**
 * 触发一次 Markdown 下载。
 *
 * `URL.revokeObjectURL` 延后执行：部分浏览器在 click 后还要读一段时间，立刻 revoke
 * 会让下载变成「失败」或空文件。返回是否真的发起了下载（没有 DOM 时 false）。
 */
export function downloadMarkdownFile(filename: string, markdown: string): boolean {
  if (typeof document === 'undefined') return false;

  const blob = new Blob([markdown], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  anchor.rel = 'noopener';
  anchor.style.display = 'none';

  document.body.appendChild(anchor);
  try {
    anchor.click();
  } finally {
    anchor.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10_000);
  }
  return true;
}
