/**
 * 「过程」摘要文案的纯逻辑。
 *
 * 一轮回答里，思考与工具调用属于「过程」，最终回答才是结果。简洁模式下整个过程收成
 * 一行，这一行必须是**动作**而不是工具名——`tavily_search` 对用户没有意义，
 * 「联网搜索」才有。文案规则放在这里而不是组件里：`vitest` 跑 node 环境，纯函数可以
 * 直接覆盖，组件只负责摆图标与文字。
 */

/**
 * 工具名 → 动作短语（过去式，与「刚发生了什么」的叙述一致）。
 *
 * 命名约定按前缀兜底：MCP 工具名是 `<server>__<tool>`，本地工具就是函数名。
 * 没登记的工具回落到「已调用工具」，而不是把内部名字甩给用户。
 */
const TOOL_ACTIONS: Record<string, string> = {
  get_current_time: '查询了时间',
  tavily_search: '联网搜索',
  knowledge_search: '检索知识库',
  ask_user_clarification: '向你提问',
  bash: '执行了命令',
  run_command: '执行了命令',
  execute_command: '执行了命令',
  shell: '执行了命令',
  read_file: '已读取文件',
  write_file: '已写入文件',
  edit_file: '修改了文件',
  list_files: '列出了文件',
  grep: '搜索了代码',
};

/** 按工具名前缀猜动作：`read_*` / `write_*` / `edit_*` / `search_*` 这类命名很常见。 */
const PREFIX_ACTIONS: ReadonlyArray<[string, string]> = [
  ['read', '已读取文件'],
  ['write', '已写入文件'],
  ['edit', '修改了文件'],
  ['update', '修改了文件'],
  ['search', '搜索'],
  ['list', '列出了文件'],
  ['run', '执行了命令'],
  ['exec', '执行了命令'],
  ['bash', '执行了命令'],
  ['del', '删除了内容'],
  ['create', '新建了内容'],
];

/** 折叠条里最多列出几个动作，超出用「等」收口——一行文字不能无限长。 */
export const TRACE_ACTION_LIMIT = 3;

/** 工具名 → 动作短语。 */
export function describeToolAction(toolName: string): string {
  if (!toolName) return '已调用工具';
  const known = TOOL_ACTIONS[toolName];
  if (known) return known;

  // MCP 工具取分隔符之后那段再猜，`mcp__github__read_file` → `read_file`
  const short = toolName.includes('__') ? toolName.split('__').pop()! : toolName;
  if (short && short !== toolName) {
    const knownShort = TOOL_ACTIONS[short];
    if (knownShort) return knownShort;
  }

  const lower = short.toLowerCase();
  for (const [prefix, action] of PREFIX_ACTIONS) {
    if (lower.startsWith(prefix)) return action;
  }
  return '已调用工具';
}

/** 把多次工具调用的动作去重后拼成一句：`执行了命令，已读取文件`。 */
export function describeToolActions(toolNames: readonly string[]): string {
  const unique = [...new Set(toolNames.map(describeToolAction))].filter(Boolean);
  if (unique.length <= TRACE_ACTION_LIMIT) return unique.join('，');
  return `${unique.slice(0, TRACE_ACTION_LIMIT).join('，')} 等 ${unique.length} 项`;
}

export interface TraceSummaryInput {
  /** 已经结算的思考耗时（秒）；0 表示这一组里没有思考。 */
  thinkingSeconds?: number;
  /** 本组里的工具名（可重复，按调用次数算）。 */
  toolNames?: readonly string[];
  /** 其中失败/被拒绝/中断的次数。 */
  failedCount?: number;
  /** 本组仍在执行。 */
  running?: boolean;
}

/**
 * 折叠条文案。
 *
 * 例：`已思考 2s · 执行了命令，联网搜索`。
 * 什么都没有时给一个中性短语，而不是空字符串——空标题的折叠条没有可点的目标，
 * 读屏也读不出这是「过程」。
 */
export function describeTraceSummary({
  thinkingSeconds = 0,
  toolNames = [],
  failedCount = 0,
  running = false,
}: TraceSummaryInput): string {
  const segments: string[] = [];

  if (thinkingSeconds > 0) segments.push(`已思考 ${thinkingSeconds}s`);
  if (toolNames.length > 0) segments.push(describeToolActions(toolNames));
  if (failedCount > 0) segments.push(`${failedCount} 个失败`);

  if (segments.length === 0) return running ? '正在处理…' : '过程';
  return segments.join(' · ');
}

/** 折叠条的读屏文案：只说「展开/收起」不够，要带上这次展开的是什么。 */
export function traceToggleLabel(summary: string, open: boolean): string {
  return `${open ? '收起' : '展开'}过程：${summary}`;
}
