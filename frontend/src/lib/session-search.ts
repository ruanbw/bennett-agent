/**
 * 会话搜索：标题与**消息正文**。
 *
 * 此前侧边栏只拿 `session.title.includes(query)` 过滤，用户明明记得对话里说过某个词，
 * 却搜不到那个会话——标题是模型自动生成的摘要，和用户记忆里的关键词经常对不上。
 *
 * ## 语义（写死在这里，组件不参与判断）
 *
 * - **多词是与（AND）**：`向量 检索` 要求两个词都出现，任一缺失即不命中。判定与片段
 *   都落在**同一条消息**上：片段必须能自证命中原因，跨消息拼出来的「命中」无法展示
 *   给用户看。
 * - **大小写不敏感**，按子串匹配（中文不需要分词；不引入分词器就没有「搜不到原词」的
 *   失败模式，代价是英文只能按子串匹配）。
 * - 正文命中**按会话聚合**：片段取最近一条命中的消息（越新的上下文越可能是用户要找的），
 *   同时给出命中的消息条数。
 * - **工具调用也参与搜索**（名称 / 参数 / 结果），但片段会标出来源（`工具 search_docs`）：
 *   工具块在界面上默认折叠，不标来源的话用户点开也看不到关键词在哪。
 * - **不搜 `thinking`**：思考链在界面上默认折叠、体积不可控，命中它会让用户看到一条
 *   自己点开也找不到关键词的会话。搜索范围与「用户看得见的正文」保持一致。
 *
 * ## 为什么放在 lib 而不是组件里
 *
 * 片段截取、偏移量计算、AND 判定都容易写出「高亮位置整体偏一位」这类错误，而 vitest 的
 * node 环境跑不了组件。抽成纯函数后可以直接断言 `snippet.text.slice(range)` 就是命中的词。
 */

import { db } from '../db';
import type { MessageRecord } from '../types';

/** 单次查询最多认 8 个词：再多也不会更精确，只会让每个片段多算 N 遍。 */
const MAX_QUERY_TOKENS = 8;
/** 每个词在片段里最多高亮多少处：超长回复里同一个词可能出现上百次。 */
const MAX_HIGHLIGHTS_PER_TOKEN = 20;
/** 单个词的出现次数封顶：只用于「命中密度」，数到 200 已经没有排序价值。 */
const COUNT_LIMIT = 200;

/** 片段默认长度与命中词前的上下文长度（字符，按折叠空白后计）。 */
export const SNIPPET_MAX_LENGTH = 120;
export const SNIPPET_LEADING = 32;

export interface TextRange {
  start: number;
  end: number;
}

export interface TextMatch {
  /** 每个词首次出现的位置，与传入的 tokens 同序。 */
  firstPositions: number[];
  /** 所有词出现次数之和（封顶 `COUNT_LIMIT`）。 */
  count: number;
}

export interface SearchSnippet {
  /** 已折叠空白、首尾可能带省略号的展示文本。 */
  text: string;
  /** 相对于 `text` 的高亮区间，已排序且互不重叠。 */
  ranges: TextRange[];
  /** 整个正文里的命中次数（不是片段内的）。 */
  count: number;
}

export interface ContentMatch {
  sessionId: string;
  /** 最近一条命中消息的片段。 */
  snippet: SearchSnippet;
  /**
   * 命中来源：`工具 search_docs` / `提问`；`null` = 消息正文。
   *
   * 界面必须显示它：工具结果在默认折叠的区域里，只给片段而不说来源，用户会以为
   * 命中的是自己的某句话。
   */
  label: string | null;
  /** 这个会话里有几条消息命中（同一会话可能命中多条）。 */
  messageCount: number;
  /** 最近一条命中消息的时间，用于「片段取最新」的判定（也便于将来排序）。 */
  newestMatchedAt: number;
}

/**
 * 把输入拆成词表。
 *
 * 用 `toLowerCase()` 统一大小写而不是 `toLocaleLowerCase()`：后者在土耳其语环境下会把
 * `I` 变成 `ı`，与用户看到的正文对不上，高亮偏移随之错位。
 */
export function parseSearchQuery(raw: string): string[] {
  const tokens = new Set<string>();
  for (const part of raw.toLowerCase().split(/\s+/)) {
    if (part) tokens.add(part);
    if (tokens.size >= MAX_QUERY_TOKENS) break;
  }
  return [...tokens];
}

/** 折叠换行/连续空白并去掉首尾空白：markdown 正文里的缩进与空行在单行片段里没有意义。 */
export function collapseWhitespace(text: string): string {
  return text.replace(/\s+/g, ' ').trim();
}

/** 查询词是字面量：`[` / `(` 等不能被当成正则语法。 */
function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

interface TokenHit {
  index: number;
  length: number;
}

/**
 * 找出一个查询词在原文里的所有命中位置（大小写不敏感）。
 *
 * 用带 `i` 的正则而不是「把整段文本 toLowerCase 后 indexOf」：后者在 `İ` 这类
 * 小写后长度会变的字符上会让**整段文本**的高亮偏移失效（旧实现直接放弃全部高亮），
 * 正则的 `match.index` 始终落在原文坐标系里。匹配仍是子串字面量语义。
 */
function scanToken(text: string, token: string, limit: number): TokenHit[] {
  if (!token) return [];
  const pattern = new RegExp(escapeRegExp(token), 'gi');
  const hits: TokenHit[] = [];
  let match: RegExpExecArray | null;
  while (hits.length < limit && (match = pattern.exec(text)) !== null) {
    hits.push({ index: match.index, length: match[0].length });
    // 空匹配会原地打转；查询词解析阶段已过滤空词，这里只是防御
    if (match[0].length === 0) pattern.lastIndex += 1;
  }
  return hits;
}

/**
 * AND 匹配：所有词都出现才算命中。
 *
 * 返回的是首次出现位置与总出现次数；任一缺失返回 null。调用方据此区分「命中」与
 * 「未命中」，不需要自己再写一遍 includes。
 */
export function matchText(text: string, tokens: readonly string[]): TextMatch | null {
  if (tokens.length === 0 || !text) return null;
  const firstPositions: number[] = [];
  let count = 0;

  for (const token of tokens) {
    const hits = scanToken(text, token, COUNT_LIMIT);
    if (hits.length === 0) return null;
    firstPositions.push(hits[0].index);
    count += hits.length;
  }

  return { firstPositions, count };
}

/**
 * 计算高亮区间（大小写不敏感），已排序且合并重叠。
 *
 * 区间始终基于原文偏移（见 `scanToken`），所以不会出现「大小写折叠改变长度后整段
 * 错位」的问题；重叠词由 `mergeRanges` 合并。
 */
export function highlightRanges(text: string, tokens: readonly string[]): TextRange[] {
  if (!text || tokens.length === 0) return [];

  const ranges: TextRange[] = [];
  for (const token of tokens) {
    for (const hit of scanToken(text, token, MAX_HIGHLIGHTS_PER_TOKEN)) {
      ranges.push({ start: hit.index, end: hit.index + hit.length });
    }
  }
  return mergeRanges(ranges);
}

function mergeRanges(ranges: TextRange[]): TextRange[] {
  if (ranges.length <= 1) return ranges;
  ranges.sort((a, b) => a.start - b.start || a.end - b.end);

  const merged: TextRange[] = [ranges[0]];
  for (let i = 1; i < ranges.length; i += 1) {
    const last = merged[merged.length - 1];
    const next = ranges[i];
    if (next.start <= last.end) {
      if (next.end > last.end) last.end = next.end;
    } else {
      merged.push(next);
    }
  }
  return merged;
}

export interface SnippetOptions {
  maxLength?: number;
  leading?: number;
}

/**
 * 截出以「最早出现的那个词」为中心的片段。
 *
 * 取最早出现的词而不是第一个 token：`/检索 向量/` 里先出现的是「检索」，片段就该
 * 落在它周围。所有 token 都必须存在（AND），否则返回 null。
 */
export function buildSnippet(
  text: string,
  tokens: readonly string[],
  { maxLength = SNIPPET_MAX_LENGTH, leading = SNIPPET_LEADING }: SnippetOptions = {},
): SearchSnippet | null {
  if (tokens.length === 0) return null;
  const flat = collapseWhitespace(text);
  if (!flat) return null;

  const match = matchText(flat, tokens);
  if (!match) return null;

  const anchor = Math.min(...match.firstPositions);
  let start = Math.max(0, anchor - leading);
  let end = Math.min(flat.length, start + maxLength);
  // 靠近结尾时向前补足，避免片段只剩小半行
  if (end - start < maxLength) start = Math.max(0, end - maxLength);

  const prefix = start > 0 ? '…' : '';
  const suffix = end < flat.length ? '…' : '';
  const body = flat.slice(start, end);
  const offset = prefix.length;

  return {
    text: prefix + body + suffix,
    ranges: highlightRanges(body, tokens).map((range) => ({
      start: range.start + offset,
      end: range.end + offset,
    })),
    count: match.count,
  };
}

/** 单条消息里的一段可搜索文本；`label` 说明命中来自哪里（null = 正文）。 */
interface SearchSegment {
  label: string | null;
  text: string;
}

/**
 * 工具结果只搜前 20k 字符。
 *
 * 单个工具结果可以到几百 KB（网页正文、文档），逐条全文扫描会让每次按键都变慢；
 * 20k 已经覆盖绝大多数「我见过某段内容」的场景，超出部分搜索不到是明确取舍。
 */
export const MAX_TOOL_SEARCH_CHARS = 20_000;

/**
 * 一条消息参与搜索的片段，按「优先展示」的顺序：正文 → 待答提问 → 工具调用。
 *
 * 提问只存在 `clarification.question` 里、正文往往是空的（见 chat-persistence 的
 * `renderClarification`），不并进来就会出现「提问里明明有这个说法却搜不到」。
 * 工具调用包含名称、参数与结果：搜工具名本身也是合理需求（「那次 search_docs 返回了
 * 什么」）。
 */
function searchableSegments(
  message: Pick<MessageRecord, 'content' | 'clarification' | 'toolCalls'>,
): SearchSegment[] {
  const segments: SearchSegment[] = [];

  const content = message.content.trim();
  if (content) segments.push({ label: null, text: content });

  const question = message.clarification?.question?.trim();
  if (question) segments.push({ label: '提问', text: question });

  for (const toolCall of message.toolCalls ?? []) {
    const parts = [toolCall.toolName, toolCall.argsText, toolCall.result ?? '']
      .map((part) => part?.trim() ?? '')
      .filter(Boolean)
      .join('\n');
    if (!parts) continue;
    segments.push({
      label: `工具 ${toolCall.toolName}`,
      text: parts.length > MAX_TOOL_SEARCH_CHARS ? parts.slice(0, MAX_TOOL_SEARCH_CHARS) : parts,
    });
  }

  return segments;
}

/** 按片段顺序找第一个「所有词都在场」的片段；正文命中优先于工具命中。 */
function findMessageMatch(
  message: Pick<MessageRecord, 'content' | 'clarification' | 'toolCalls'>,
  tokens: readonly string[],
): { snippet: SearchSnippet; label: string | null } | null {
  for (const segment of searchableSegments(message)) {
    const snippet = buildSnippet(segment.text, tokens);
    if (snippet) return { snippet, label: segment.label };
  }
  return null;
}

export interface SearchContentOptions {
  /** 只在这些会话里搜——用 `sessionId` 索引查，不扫全表（列表外的墓碑会话自然被排除）。 */
  sessionIds: readonly string[];
  signal?: AbortSignal;
}

/** 中止哨兵：`each()` 只能靠抛异常提前结束，用它把「用户又敲了一个字」和真错误区分开。 */
const ABORT = Symbol('session-search-aborted');

/**
 * 在给定会话里搜正文，返回按会话聚合的命中。
 *
 * 实现要点：
 * - 走 `sessionId` 索引，只读候选会话的消息；会话被删除（墓碑）时它已不在候选列表里。
 * - 逐条 `each` 而不 `toArray`：只保留每个会话最好的一条片段，内存与命中数成正比，
 *   而不是与库里的消息总量成正比。
 * - `signal` 中止后立即放弃结果：输入框每敲一个字都会换一次查询，旧扫描的结果绝不能
 *   覆盖新查询的（否则列表会闪回上一次的内容）。
 */
export async function searchSessionContent(
  tokens: readonly string[],
  { sessionIds, signal }: SearchContentOptions,
): Promise<Map<string, ContentMatch>> {
  const matches = new Map<string, ContentMatch>();
  if (tokens.length === 0 || sessionIds.length === 0 || signal?.aborted) return matches;

  // `parseSearchQuery` 已经小写化了，但这里再归一化一次：大小写不敏感是对调用方的
  // 承诺，靠「上一层记得转」会让直接调用本函数的人踩到「明明有这个词却搜不到」。
  const normalizedTokens = tokens.map((token) => token.toLowerCase());

  try {
    await db.messages
      .where('sessionId')
      .anyOf([...sessionIds])
      .each((message) => {
        if (signal?.aborted) throw ABORT;

        const match = findMessageMatch(message, normalizedTokens);
        if (!match) return;

        const existing = matches.get(message.sessionId);
        if (!existing) {
          matches.set(message.sessionId, {
            sessionId: message.sessionId,
            snippet: match.snippet,
            label: match.label,
            messageCount: 1,
            newestMatchedAt: message.createdAt,
          });
          return;
        }

        // 片段与来源标签必须一起换：只换片段会把上一个片段的「工具 search_docs」
        // 留在新的正文片段前面。
        const isNewer = message.createdAt >= existing.newestMatchedAt;
        matches.set(message.sessionId, {
          sessionId: message.sessionId,
          snippet: isNewer ? match.snippet : existing.snippet,
          label: isNewer ? match.label : existing.label,
          messageCount: existing.messageCount + 1,
          newestMatchedAt: Math.max(existing.newestMatchedAt, message.createdAt),
        });
      });
  } catch (err) {
    // 中止不是错误：Dexie 会把 each 里的抛出原样 reject 出来
    if (err === ABORT) return new Map();
    throw err;
  }

  return matches;
}
