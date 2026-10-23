/**
 * 「过程」摘要文案的纯逻辑。
 *
 * 一轮回答里，思考与工具调用属于「过程」，最终回答才是结果。折叠起来的那一行必须是
 * **动作**而不是工具名——`tavily_search` 对用户没有意义，「已搜索网页」才有。
 *
 * 词表与拼接规则对齐 DSH 的 `message.stepProcess.*`（见 `lib/step-process.ts` 顶部的
 * 说明）。这里保留 `describeToolActions` 作为「一串工具名 → 一句话」的入口：
 * 组件现在走 `step-process.ts` 的分类表，但历史调用方与它自己的单测仍需要它。
 */

import {
  classifyActivity,
  doneProcessTitle,
  formatRunDurationText,
  processTitle,
  rankActivities,
  runningProcessTitle,
  summarizeActivities,
  type ActivityKind,
} from './step-process';

/**
 * 工具名 → 动作短语（过去式，与「刚发生了什么」的叙述一致）。
 *
 * 走与组头同一张分类表：同一轮里组头说「已读取文件」，单行工具行就不该说「读取」。
 * 认不出的工具回落「已调用工具」，而不是把内部名字甩给用户。
 */
export function describeToolAction(toolName: string): string {
  return doneProcessTitle(classifyActivity(toolName));
}

/** 折叠条里最多列出几个动作，超出用「等」收口——一行文字不能无限长。 */
export const TRACE_ACTION_LIMIT = 3;

/**
 * 把多次工具调用的动作去重后拼成一句：`已读取文件并写入文件`。
 *
 * 注意规则与旧版不同：不再是 `A，B 等 5 项`，而是 DSH 的「两项用并、三项用逗号、
 * 超过三类加等」。原因见 `processTitle` 的注释：用户要知道「干了什么」，
 * 不是「调了几次」。
 */
export function describeToolActions(toolNames: readonly string[]): string {
  if (toolNames.length === 0) return '';
  return processTitle(summarizeActivities(toolNames));
}

export interface TraceSummaryInput {
  /**
   * 已经结算的思考耗时（秒）。
   *
   * 仍然接受但**不再出现在文案里**：DSH 的组头只说「做了什么」，「花了多久」属于
   * 整轮的耗时（渲染在消息底部的操作栏里，见 `thread.aui.tsx`）。保留这个字段是为了
   * 让老调用方不因类型检查炸掉，而不是为了继续渲染它。
   */
  thinkingSeconds?: number;
  /** 本组里的工具名（可重复，按调用次数算）。 */
  toolNames?: readonly string[];
  /** 其中失败/被拒绝/中断的次数。 */
  failedCount?: number;
  /** 本组仍在执行。 */
  running?: boolean;
}

/**
 * 折叠条文案（**不含**「已思考 Ns」这种耗时前缀）。
 *
 * 耗时属于「这一轮花了多久」，挂在消息底部的操作栏里，
 * 不该混进「做了什么」这一句里——混在一起读起来是「已思考 2s 并执行了命令」，
 * 两个不同量纲的东西用「并」连接。
 *
 * 什么都没有时给一个中性短语，而不是空字符串：空标题的折叠条没有可点的目标，
 * 读屏也读不出这是「过程」。
 */
export function describeTraceSummary({
  toolNames = [],
  failedCount = 0,
  running = false,
}: TraceSummaryInput): string {
  const base = toolNames.length > 0 ? describeToolActions(toolNames) : '';
  const segments: string[] = [];
  if (base) segments.push(base);
  if (failedCount > 0) segments.push(`${failedCount} 个失败`);

  if (segments.length === 0) return running ? '正在处理…' : '过程';
  return segments.join('，');
}

/** 折叠条的读屏文案：只说「展开/收起」不够，要带上这次展开的是什么。 */
export function traceToggleLabel(summary: string, open: boolean): string {
  return `${open ? '收起' : '展开'}过程：${summary}`;
}

/**
 * 思考行的预览摘要。
 *
 * - 运行中取**最后一个已完成段落的首行**：正在写的这一段还是半句话，拿它当预览会
 *   一路闪变；最后一个写完的段落首行稳定，也最接近「他刚想明白了什么」。
 * - 已结束取**首行**：完整的思考已经定型，首行是它的主题句。
 * - 去掉 `**`：Markdown 粗体标记在纯文本单行里只会显示成星号。
 *
 * 返回空串表示没有可展示的预览（调用方据此隐藏分隔点与摘要）。
 */
export function reasoningPreview(text: string | undefined, running: boolean): string {
  if (!text) return '';
  const source = running ? latestCompletedParagraphFirstLine(text) : firstLine(text);
  return stripMarkdownEmphasis(source);
}

/** 文本第一个换行之前的内容。 */
function firstLine(text: string): string {
  const newline = text.indexOf('\n');
  return newline === -1 ? text : text.slice(0, newline);
}

/**
 * 最后一个「已写完」段落的首行。
 *
 * 「写完」的判定（照抄 DSH）：段落被空行分隔符终结，或者段落内部已经有换行。
 * 正在流式输入的那一段既没有后续分隔符、又可能一行都没换行，于是被跳过——
 * 这正是我们要的：**预览不要跟着每个 token 闪**。
 *
 * 代价是「整段思考还没有任何换行」时预览为空（思考行只显示「思考」两个字）。
 * 这是刻意的取舍：一个还在增长的半句话当预览，会在几秒内抖动十几次。
 */
function latestCompletedParagraphFirstLine(text: string): string {
  const separator = /\r?\n(?:[\t ]*\r?\n)+/g;
  /** 每个段落的正文，以及它是否被空行终结（终结 = 一定写完了）。 */
  const paragraphs: Array<{ body: string; terminated: boolean }> = [];
  let paragraphStart = 0;
  for (let match = separator.exec(text); match !== null; match = separator.exec(text)) {
    paragraphs.push({ body: text.slice(paragraphStart, match.index), terminated: true });
    paragraphStart = match.index + match[0].length;
  }
  // 分隔符之后剩下的最后一段：它可能还在输入中
  paragraphs.push({ body: text.slice(paragraphStart), terminated: false });

  for (let index = paragraphs.length - 1; index >= 0; index -= 1) {
    const { body, terminated } = paragraphs[index]!;
    // 没被终结的末段必须自己带换行，才说明它的第一行已经写完
    if (!terminated && !body.includes('\n')) continue;
    const newline = body.indexOf('\n');
    const candidate = (newline === -1 ? body : body.slice(0, newline)).trim();
    if (candidate !== '') return candidate;
  }
  return '';
}

/** 去掉 Markdown 粗体标记与首尾空白。 */
function stripMarkdownEmphasis(text: string): string {
  return text.replaceAll('**', '').trim();
}

/**
 * 运行中组头的「详情」：当前动作的一行描述。
 *
 * 优先级与 DSH 的 `LIVE_TOOL_DETAIL_KEYS` 一致——取第一个有内容的主参数。
 * 参数还在流式生成（JSON 不完整）时拿不到就返回空串，调用方据此只显示动作标题，
 * 而不是显示半截 `{"que`。
 */
const LIVE_DETAIL_KEYS = [
  'title',
  'description',
  'objective',
  'task',
  'task_name',
  'name',
  'question',
  'questions',
  'prompt',
  'message',
  'command',
  'cmd',
  'queries',
  'query',
  'pattern',
  'url',
  'uri',
  'file_path',
  'path',
  'target',
  'action',
] as const;

/** 详情一行最多这么长；再长就截断加省略号（组头是一行文字，不能换行）。 */
export const LIVE_DETAIL_MAX_CHARS = 160;

export function liveToolDetail(argsText: string | undefined): string {
  if (!argsText) return '';
  let parsed: unknown;
  try {
    parsed = JSON.parse(argsText);
  } catch {
    // 流式过程中 argsText 可能还不是合法 JSON：此时没有可靠详情，宁可不显示
    return '';
  }
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) return '';

  const record = parsed as Record<string, unknown>;
  for (const key of LIVE_DETAIL_KEYS) {
    const value = record[key];
    const detail = normalizeDetailText(value);
    if (detail !== '') return detail;
  }
  return '';
}

/**
 * 参数值 → 一行文本。
 *
 * 数组（`queries` / `questions`）取第一个可读项：组头只能放一行，列出全部会溢出。
 */
function normalizeDetailText(value: unknown): string {
  if (typeof value === 'string') return clampDetail(value);
  if (Array.isArray(value)) {
    for (const item of value) {
      if (typeof item === 'string' && item.trim() !== '') return clampDetail(item);
      // ask_user_question 的 questions 是对象数组，取其中的 question 字段
      if (item && typeof item === 'object') {
        const question = (item as Record<string, unknown>).question;
        if (typeof question === 'string' && question.trim() !== '') return clampDetail(question);
      }
    }
  }
  return '';
}

/** 折叠成一行、去首尾空白、超长截断。 */
function clampDetail(text: string): string {
  const flat = text.replace(/\s+/g, ' ').trim();
  if (flat.length <= LIVE_DETAIL_MAX_CHARS) return flat;
  // 用 Array.from 按码点切，避免劈开 emoji / 代理对
  const chars = Array.from(flat);
  return `${chars.slice(0, LIVE_DETAIL_MAX_CHARS - 1).join('').trimEnd()}…`;
}

/**
 * 已结束的一轮里，把「这一轮干了什么」拼成一行完整摘要。
 *
 * 只有工具动作（没有思考耗时、没有失败数）时就是组头本身。保留这个组合入口是给
 * 「把整轮折叠成一行」的场景用的：它要的是**一句**话，不是一个数组。
 */
export function describeCompletedTurn({
  toolNames = [],
  failedCount = 0,
  elapsedMs,
}: {
  toolNames?: readonly string[];
  failedCount?: number;
  elapsedMs?: number | null;
}): string {
  const segments: string[] = [];
  const actions = describeToolActions(toolNames);
  if (actions) segments.push(actions);
  if (failedCount > 0) segments.push(`${failedCount} 个失败`);
  if (elapsedMs !== undefined && elapsedMs !== null && Number.isFinite(elapsedMs)) {
    segments.push(formatRunDurationText(Math.max(1000, elapsedMs)));
  }
  return segments.join('，');
}

/** 组头右侧的「等」/排序入口都从 `step-process` 来，这里只导出一份引用便于测试对齐。 */
export { rankActivities };

/** 过程组组头的完整文案（含详情与失败数）。 */
export interface TraceGroupHeader {
  /** 组头标题（已拼好详情；用于 aria-label）。 */
  title: string;
  /** 只含动作的部分（渲染时详情单独套淡色）。 */
  summary: string;
  /** 实际展示的详情；空串表示不展示。 */
  detail: string;
  /** 失败数尾注（以「，」开头）；空串表示没有失败。 */
  failureNote: string;
}

/**
 * 过程组组头的完整文案（含详情与失败数）。
 *
 * 抽成纯函数是因为它是这一版最容易出错的组合：运行中/已结束两套词表 × 要不要带详情
 * × 有没有失败。「详情」只有在**运行中**且策略允许时才追加——已结束的组头里塞
 * 「已读取文件 · /tmp/a.ts」会把一行撑成两句，而用户已经能从展开的工具行看到路径。
 *
 * 失败的组即使折叠着也要能看出这轮不顺利，所以失败数追加在最后，且用文字而不是
 * 只靠颜色（色弱读不出）。
 */
export function traceGroupHeader({
  running,
  activity,
  detail = '',
  toolNames = [],
  failedCount = 0,
  showLiveDetail = false,
}: {
  running: boolean;
  activity: ActivityKind;
  /** 当前动作的详情（命令、路径、查询词…）；拿不到时为空串。 */
  detail?: string;
  /** 本组里的工具名（运行中也要传：失败数要按全部调用算）。 */
  toolNames?: readonly string[];
  failedCount?: number;
  /** 当前档位是否允许运行中带详情。 */
  showLiveDetail?: boolean;
}): TraceGroupHeader {
  const summary = running
    ? runningProcessTitle(activity)
    : processTitle(summarizeActivities(toolNames));
  // 已结束不带详情：那一行要说的是「干了什么」，细节在展开的工具行里
  const shownDetail = running && showLiveDetail ? detail : '';
  const title = shownDetail === '' ? summary : `${summary} · ${shownDetail}`;
  const failureNote = failedCount > 0 ? `，${failedCount} 个失败` : '';
  return { title, summary, detail: shownDetail, failureNote };
}
