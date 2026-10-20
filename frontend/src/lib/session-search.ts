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

/**
 * AND 匹配：所有词都出现才算命中。
 *
 * 返回的是首次出现位置与总出现次数；任一缺失返回 null。调用方据此区分「命中」与
 * 「未命中」，不需要自己再写一遍 includes。
 */
export function matchText(text: string, tokens: readonly string[]): TextMatch | null {
  if (tokens.length === 0 || !text) return null;
  const lower = text.toLowerCase();
  const firstPositions: number[] = [];
  let count = 0;

  for (const token of tokens) {
    const first = lower.indexOf(token);
    if (first === -1) return null;
    firstPositions.push(first);

    let from = first;
    let seen = 0;
    while (seen < COUNT_LIMIT) {
      const at = lower.indexOf(token, from);
      if (at === -1) break;
      count += 1;
      seen += 1;
      // +token.length：同一位置上的自重叠（如 "aaa" 里的 "aa"）不重复计数
      from = at + token.length;
    }
  }

  return { firstPositions, count };
}

/**
 * 计算高亮区间（大小写不敏感），已排序且合并重叠。
 *
 * 大小写转换改变了字符串长度（`ß` → `ss`）时直接放弃高亮：偏移量与原文对不上，
 * 宁可不高亮也不能把标记打错位置。
 */
export function highlightRanges(text: string, tokens: readonly string[]): TextRange[] {
  if (!text || tokens.length === 0) return [];
  const lower = text.toLowerCase();
  if (lower.length !== text.length) return [];

  const ranges: TextRange[] = [];
  for (const token of tokens) {
    let from = 0;
    for (let i = 0; i < MAX_HIGHLIGHTS_PER_TOKEN; i += 1) {
      const at = lower.indexOf(token, from);
      if (at === -1) break;
      ranges.push({ start: at, end: at + token.length });
      from = at + token.length;
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

/**
 * 一条消息参与搜索的文本：正文 + 待答提问。
 *
 * 提问只存在 `clarification.question` 里、正文往往是空的（见 chat-persistence 的
 * `renderClarification`），不并进来就会出现「提问里明明有这个说法却搜不到」。
 */
function searchableText(message: Pick<MessageRecord, 'content' | 'clarification'>): string {
  const question = message.clarification?.question?.trim();
  if (!question) return message.content;
  return message.content.trim() ? `${message.content} ${question}` : question;
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

  try {
    await db.messages
      .where('sessionId')
      .anyOf([...sessionIds])
      .each((message) => {
        if (signal?.aborted) throw ABORT;

        const snippet = buildSnippet(searchableText(message), tokens);
        if (!snippet) return;

        const existing = matches.get(message.sessionId);
        if (!existing) {
          matches.set(message.sessionId, {
            sessionId: message.sessionId,
            snippet,
            messageCount: 1,
            newestMatchedAt: message.createdAt,
          });
          return;
        }

        const isNewer = message.createdAt >= existing.newestMatchedAt;
        matches.set(message.sessionId, {
          sessionId: message.sessionId,
          snippet: isNewer ? snippet : existing.snippet,
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
