/**
 * 侧边栏搜索的 React 接线：防抖 + 中止旧查询 + 输入过程中保留上一次结果。
 *
 * 三条规则：
 * 1. **防抖**：每敲一个字都扫一遍 IndexedDB 没有必要，180ms 足够避开连续输入。
 * 2. **旧查询必须中止**：`searchSessionContent` 接受 `AbortSignal`，否则慢查询的结果回来
 *    会覆盖新查询的（列表闪回上一次内容）。
 * 3. **输入过程中保留上一次结果**（stale-while-revalidate）（仅限同一串词的前后缀变化）。
 *    否则每敲一个字列表都会先塌成「只剩标题命中」再弹回来，行高跟着跳。换了一个完全
 *    不同的词时不复用旧结果——那会短暂显示一批与当前输入无关的会话。
 *
 * `pending` 由「已完成结果的 key !== 当前 key」推导，不在 effect 里同步 setState：
 * 同一次渲染里就能得出正确状态，也就少一轮级联渲染。
 *
 * 搜索是纯本地读，失败只可能是数据库异常（升级被其他标签页阻塞、配额等），
 * 因此把原因交给调用方展示，而不是静默清空列表。
 */

import { useCallback, useEffect, useMemo, useState } from 'react';

import { parseSearchQuery, searchSessionContent, type ContentMatch } from '../lib/session-search';

/** 停下输入多久后才真正扫库。 */
export const SEARCH_DEBOUNCE_MS = 180;

export interface SessionContentSearch {
  /** 当前输入的词表：与输入**同步**，用于标题高亮与列表过滤。 */
  tokens: string[];
  /** 正文命中；`pending` 时可能是上一串相近查询的结果（见上）。 */
  matches: Map<string, ContentMatch>;
  /** 防抖中或扫描中（结果尚未落地）。 */
  pending: boolean;
  /** 扫描失败的可读原因；成功一轮后清空。 */
  error: string | null;
  /** 重新扫一遍当前词表（错误态里的「重新搜索」）。 */
  retry: () => void;
}

interface SearchResult {
  /** 这份结果对应的查询 key（词表用 NUL 连接）。 */
  key: string;
  matches: Map<string, ContentMatch>;
  error: string | null;
}

const EMPTY_MATCHES: Map<string, ContentMatch> = new Map();
const EMPTY_RESULT: SearchResult = { key: '', matches: EMPTY_MATCHES, error: null };

/** 「同一串词的继续输入/退格」才复用旧结果；换词不复用。 */
function isRelatedQuery(previous: string, current: string): boolean {
  if (!previous || !current) return false;
  return current.startsWith(previous) || previous.startsWith(current);
}

export function useSessionContentSearch(
  rawQuery: string,
  sessionIds: readonly string[],
): SessionContentSearch {
  const tokens = useMemo(() => parseSearchQuery(rawQuery), [rawQuery]);
  const tokensKey = tokens.join('\u0000');
  // 会话列表变化（新建/删除/同步拉到新会话）时也要重搜：候选集合变了。
  const idsKey = sessionIds.join('\u0000');

  const [result, setResult] = useState<SearchResult>(EMPTY_RESULT);
  // 失败后重试：只递增这个计数，effect 的其余依赖不变也能重新跑一轮
  const [attempt, setAttempt] = useState(0);
  const retry = useCallback(() => setAttempt((count) => count + 1), []);

  useEffect(() => {
    if (!tokensKey) return undefined;

    // tokens / sessionIds 都是 useMemo 出来的稳定引用（key 变化时引用才变），
    // 直接给 effect 用，不需要 ref 兜底。
    const controller = new AbortController();
    const timer = setTimeout(() => {
      void (async () => {
        try {
          const matches = await searchSessionContent(tokens, { sessionIds, signal: controller.signal });
          if (controller.signal.aborted) return;
          setResult({ key: tokensKey, matches, error: null });
        } catch (err) {
          if (controller.signal.aborted) return;
          console.error('搜索对话内容失败:', err);
          setResult({ key: tokensKey, matches: EMPTY_MATCHES, error: '搜索对话内容失败，请重试' });
        }
      })();
    }, SEARCH_DEBOUNCE_MS);

    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [tokensKey, idsKey, tokens, sessionIds, attempt]);

  if (tokensKey === '') {
    return { tokens, matches: EMPTY_MATCHES, pending: false, error: null, retry };
  }

  const pending = result.key !== tokensKey;
  const matches = isRelatedQuery(result.key, tokensKey) ? result.matches : EMPTY_MATCHES;

  return { tokens, matches, pending, error: pending ? null : result.error, retry };
}
