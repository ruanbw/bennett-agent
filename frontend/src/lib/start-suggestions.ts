/**
 * 空对话页的起步建议：文案、图标与能力裁剪的唯一来源。
 *
 * 为什么不把文案写死在组件里：建议必须跟**服务端实际装配的工具**一致。
 * `requiresWebSearch` 的条目只在 `/api/capabilities` 报告联网搜索可用时才展示——
 * 用户点开一条 agent 根本答不了的示例，比空着更伤（见 frontend/README.md）。
 *
 * 文案原则：
 * - 一条示例就是一次可直接发送的请求，不要写成「改写这段…」却什么都不给；用户
 *   补上正文即可，但不要再让他猜要往里填什么格式；
 * - 短到能在 2×2 栅格里读全，长到能看出这条示例到底在演示什么能力。
 */

/** 图标名而非图标组件：这个模块是纯逻辑，能被 vitest 直接覆盖，不拖 React 进来。 */
export type StartSuggestionIcon =
  | 'search'
  | 'compare'
  | 'checklist'
  | 'rewrite'
  | 'concept'
  | 'calendar'
  | 'test'
  | 'computer';

export interface StartSuggestion {
  /** 稳定 id：React key 与「换一批」的排序都靠它，不走文案。 */
  id: string;
  text: string;
  icon: StartSuggestionIcon;
  /** 只在联网搜索可用时展示；服务端能力清单是唯一判据。 */
  requiresWebSearch?: boolean;
  /** 只在「我的电脑」（隔离执行环境）可用时展示。 */
  requiresSandbox?: boolean;
}

/**
 * 建议池。顺序即默认展示顺序：联网可用时第一条就是搜索，突出这个 agent 的招牌能力；
 * 联网不可用时搜索条目整条消失，用户看到的仍是一组完整的本地能力示例。
 */
export const START_SUGGESTIONS: readonly StartSuggestion[] = [
  {
    id: 'search-industry',
    text: '搜索本周 AI 行业动态并附来源',
    icon: 'search',
    requiresWebSearch: true,
  },
  { id: 'compare-options', text: '对比两种技术方案并给出取舍表', icon: 'compare' },
  {
    id: 'search-react',
    text: '查 React 最新版本与破坏性变更',
    icon: 'search',
    requiresWebSearch: true,
  },
  { id: 'task-breakdown', text: '把需求拆成可执行的任务清单', icon: 'checklist' },
  { id: 'rewrite-note', text: '改写这段技术说明并保留原意', icon: 'rewrite' },
  { id: 'explain-concept', text: '用一个类比解释消息队列', icon: 'concept' },
  { id: 'three-day-plan', text: '按今天的日期排三天学习计划', icon: 'calendar' },
  { id: 'unit-test-cases', text: '为这个函数补一组边界单测', icon: 'test' },
  {
    // 「我的电脑」的入口示例。文案刻意不提「沙箱/虚拟机」：用户关心的是
    // 「它在我自己的机器上真的跑了一遍」，而不是我们用什么技术实现的。
    id: 'run-on-my-computer',
    text: '在我电脑上跑段代码验证这个结论',
    icon: 'computer',
    requiresSandbox: true,
  },
] as const;

/**
 * 按能力裁剪后的候选池。
 *
 * `webSearch=false` 同时也覆盖「能力清单还没回来 / 请求失败」：宁可不推荐搜索，也不要
 * 推荐一条可能必然失败的示例。失败的探测不该阻塞对话，也不该被当成「已启用」。
 */
export function availableStartSuggestions(webSearch: boolean, sandbox = false): StartSuggestion[] {
  return START_SUGGESTIONS.filter(
    (suggestion) =>
      (!suggestion.requiresWebSearch || webSearch) && (!suggestion.requiresSandbox || sandbox),
  );
}

/**
 * 取一批建议；`offset` 给「换一批」用。
 *
 * 循环取而不是随机取：随机可能在两次点击间重复同一批，用户会以为按钮没生效；
 * 循环还能保证「换一批」最终能看到池子里的全部示例。同一批内不重复。
 */
export function pickStartSuggestions(
  webSearch: boolean,
  offset = 0,
  count = 4,
  sandbox = false,
): StartSuggestion[] {
  const available = availableStartSuggestions(webSearch, sandbox);
  if (available.length === 0 || count <= 0) return [];

  const start = ((offset % available.length) + available.length) % available.length;
  const size = Math.min(count, available.length);
  return Array.from({ length: size }, (_, index) => available[(start + index) % available.length]);
}
