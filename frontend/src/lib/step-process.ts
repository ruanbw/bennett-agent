/**
 * 「工作步骤」的纯逻辑：动作分类、组标题、时长文案。
 *
 * 对齐 DSH 的两段源码：
 *   - `/tmp/dshx/dsh-client-ui-chat/lib/client.js` 的 `process-activity.js`（`activity()`
 *     的判定表）、`step-process.js`（`processTitle()` 的拼接规则）、`formatRunDuration()`；
 *   - 同文件 locale 段的 `message.stepProcess.*` 全部中英文案。
 *
 * 为什么单独一个模块：这三块都是「输入字符串，输出字符串」的规则，最容易出现
 * 「少了一个空格」「两项时忘了去重前缀」这种只有肉眼才看得出的错。放 lib 里用
 * vitest 逐条钉住，组件只负责按分类挑图标。
 */

/**
 * 工具调用归到哪一类动作。
 *
 * 顺序有意义：`activity()` 里先命中的先返回（`read_image` 必须在 `read*` 之类的
 * 前缀判断之前），下面的 if 链与 DSH 一一对应。
 */
export type ActivityKind =
  | 'thinking'
  | 'read'
  | 'readImage'
  | 'write'
  | 'search'
  | 'edit'
  | 'commands'
  | 'code'
  | 'webSearch'
  | 'webFetch'
  | 'time'
  | 'knowledge'
  | 'subagents'
  | 'plan'
  | 'questions'
  | 'tools';

/**
 * 工具名 → 动作分类。
 *
 * 本仓的工具名（`app/tools/`：`tavily_search` / `search_knowledge_base` /
 * `ask_user_clarification` / `get_current_time`）与 DSH 的原生工具名不同，所以除了
 * 照抄 DSH 的判定表，还要认本仓这一套；两者都不认时回落 `tools`（「已调用工具」），
 * 而不是把内部名字甩给用户。
 */
export function classifyActivity(toolName: string): ActivityKind {
  const name = normalizeToolName(toolName);
  if (!name) return 'tools';

  if (name === 'read') return 'read';
  if (name === 'read_image') return 'readImage';
  // 代码检索类：`grep`/`glob` 与所有 `*_inspect`
  if (name === 'grep' || name === 'glob' || name.endsWith('_inspect')) return 'search';
  if (name === 'write') return 'write';
  if (name === 'edit' || name === 'apply_patch') return 'edit';
  if (
    name === 'bash' ||
    name === 'pwsh' ||
    name === 'exec_command' ||
    name === 'write_stdin' ||
    name.startsWith('terminal_') ||
    // 本仓/常见的 shell 别名
    name === 'shell' ||
    name === 'run_command' ||
    name === 'execute_command'
  ) {
    return 'commands';
  }
  if (name === 'run_code') return 'code';
  if (name === 'web_search' || name === 'tavily_search') return 'webSearch';
  if (name === 'web_fetch' || name === 'fetch_url') return 'webFetch';
  // 本仓的「查时间」工具。DSH 没有这一类，但「查询了时间」比「已调用工具」有信息量，
  // 而这是本项目默认装配里最常被调用的工具之一（`get_current_time`）。
  if (name === 'get_current_time' || name === 'current_time') return 'time';
  if (
    name === 'knowledge_search' ||
    name === 'search_knowledge_base' ||
    name === 'knowledge_retrieve'
  ) {
    return 'knowledge';
  }
  if (name === 'subagent' || name.startsWith('subagent_')) return 'subagents';
  if (
    name === 'todo_write' ||
    name === 'create_goal' ||
    name === 'update_goal' ||
    name === 'get_goal'
  ) {
    return 'plan';
  }
  if (name === 'ask_user_question' || name === 'request_user_input') return 'questions';
  // 本仓的提问工具：它是交互协议的一部分，归「向用户提出了问题」而不是泛泛的工具
  if (name === 'ask_user_clarification') return 'questions';

  // 前缀兜底：MCP server 常把原生工具改名成 `read_file` / `search_docs` 这类，
  // 逐个登记不现实，按动词前缀猜仍然比「已调用工具」有信息量。
  // 放在最后：上面所有精确判定都已返回，这里不会盖掉它们。
  if (name.startsWith('read') || name.startsWith('get_file') || name.startsWith('view')) {
    return 'read';
  }
  if (name.startsWith('search') || name.startsWith('find') || name.startsWith('query')) {
    return 'search';
  }
  if (name.startsWith('write') || name.startsWith('create_file')) return 'write';
  if (name.startsWith('edit') || name.startsWith('update_file') || name.startsWith('patch')) {
    return 'edit';
  }
  if (name.startsWith('list') || name.startsWith('ls')) return 'search';

  return 'tools';
}

/**
 * 归一化工具名。
 *
 * MCP 工具名是 `<server>__<tool>`，末尾那一段才是有意义的动作名
 * （`mcp__fs__read_file` → `read_file`）；同时统一小写，避免大小写差异漏判定。
 */
function normalizeToolName(toolName: string): string {
  if (!toolName) return '';
  const short = toolName.includes('__') ? toolName.split('__').pop()! : toolName;
  return short.trim().toLowerCase();
}

/** 运行中的组头文案（正在做什么）。 */
const RUNNING_LABELS: Record<ActivityKind, string> = {
  thinking: '正在分析请求',
  read: '正在读取文件',
  readImage: '正在读取图片',
  write: '正在写入文件',
  search: '正在搜索代码',
  edit: '正在编辑文件',
  commands: '正在运行命令',
  code: '正在运行代码',
  webSearch: '正在搜索网页',
  webFetch: '正在访问网页',
  time: '正在查询时间',
  knowledge: '正在检索知识库',
  subagents: '正在协调子智能体',
  plan: '正在更新计划',
  questions: '等待你的操作',
  tools: '正在调用工具',
};

/**
 * 「准备…」变体：参数还在流式生成、调用尚未真正发出时用。
 *
 * `thinking` 没有「准备分析请求」这种说法（分析不是一个待发出的调用），DSH 把它
 * 映到 `prepare.tools`，这里保持同样的处理。
 */
const PREPARING_LABELS: Record<ActivityKind, string> = {
  thinking: '准备调用工具',
  read: '准备读取文件',
  readImage: '准备读取图片',
  write: '准备写入文件',
  search: '准备搜索代码',
  edit: '准备编辑文件',
  commands: '准备运行命令',
  code: '准备运行代码',
  webSearch: '准备搜索网页',
  webFetch: '准备访问网页',
  time: '准备查询时间',
  knowledge: '准备检索知识库',
  subagents: '准备协调子智能体',
  plan: '准备更新计划',
  questions: '准备提问',
  tools: '准备调用工具',
};

/** 已结束的组头文案（做了什么）。 */
const DONE_LABELS: Record<ActivityKind, string> = {
  thinking: '已完成分析',
  read: '已读取文件',
  readImage: '已读取图片',
  write: '已写入文件',
  search: '已搜索代码',
  edit: '修改了文件',
  commands: '执行了命令',
  code: '运行了代码',
  webSearch: '已搜索网页',
  webFetch: '已访问网页',
  time: '查询了时间',
  knowledge: '检索了知识库',
  subagents: '已协调子智能体',
  plan: '更新了计划',
  questions: '向用户提出了问题',
  tools: '已调用工具',
};

/** 运行中的组头文案；`preparing` 为真时用「准备…」变体。 */
export function runningProcessTitle(kind: ActivityKind, preparing = false): string {
  return (preparing ? PREPARING_LABELS : RUNNING_LABELS)[kind] ?? RUNNING_LABELS.tools;
}

/** 已结束的组头文案。 */
export function doneProcessTitle(kind: ActivityKind): string {
  return DONE_LABELS[kind] ?? DONE_LABELS.tools;
}

/** 已结束组头最多列三类动作，超出用「等」收口——一行文字不能无限长。 */
export const PROCESS_TITLE_LIMIT = 3;

/**
 * 已结束组的标题：按调用次数排序取前三类，**不带次数**。
 *
 * 拼接规则（照抄 DSH `processTitle`）：
 *   - 单项：`已读取文件`
 *   - 两项：`已读取文件并写入文件` —— 只有**两项都以「已」开头**时，才去掉第二项的「已」；
 *     `修改了文件` 不以「已」开头，所以是 `已读取文件并修改了文件` 原样连接。
 *   - 三项：`已读取文件，已写入文件，执行了命令` —— `，` 直接连接，不去前缀
 *   - 超过三类：末尾加「等」→ `已读取文件，已写入文件，执行了命令等`
 *
 * 为什么不用「已调用 3 次工具」这种计数：计数回答了「多少次」，而用户想知道的是
 * 「干了什么」。三类足够概括一轮工作，再多就没人读了。
 *
 * 注意「等」的判断用的是**类别总数**而不是截断后的三项数：三类以内不加「等」，
 * 否则会出现 `已读取文件，已写入文件等` 这种「说完了又说等」的别扭话。
 */
export function processTitle(counts: ReadonlyArray<{ kind: ActivityKind; count: number }>): string {
  const labels = rankActivities(counts).map(doneProcessTitle);
  const first = labels[0];
  // 一个工具都没调过（只有思考）时，标题是「已完成分析」而不是空串
  if (first === undefined) return DONE_LABELS.thinking;
  const second = labels[1];
  if (second === undefined) return first;

  if (labels.length === 2) {
    const sharedPrefix = '已';
    const sharePrefix = first.startsWith(sharedPrefix) && second.startsWith(sharedPrefix);
    return `${first}并${sharePrefix ? second.slice(sharedPrefix.length) : second}`;
  }

  const title = [first, ...labels.slice(1)].join('，');
  return counts.length > PROCESS_TITLE_LIMIT ? `${title}等` : title;
}

/**
 * 按次数降序、次数相同按首次出现顺序排；截到三类。
 *
 * 输入保持原顺序（调用发生顺序），所以「次数相同」时先出现的排前面——
 * 这也是 DSH 的行为，用户读到的第一个词就是他最先看到的那次动作。
 */
export function rankActivities(
  counts: ReadonlyArray<{ kind: ActivityKind; count: number }>
): ActivityKind[] {
  const indexed = counts.map((entry, index) => ({ ...entry, index }));
  indexed.sort((left, right) => right.count - left.count || left.index - right.index);
  return indexed.slice(0, PROCESS_TITLE_LIMIT).map((entry) => entry.kind);
}

/**
 * 时长片段：数字与单位分开返回，方便调用方把数字套上 tabular-nums。
 *
 * 与 `formatRunDuration` 一致：小时→分→秒从大到小，省略前导零（`1分5秒` 不写成
 * `1分05秒`），不足一分钟只给秒。
 */
export interface DurationPart {
  text: string;
  numeric: boolean;
}

/**
 * 毫秒 → 时长片段。
 *
 * `total` 向下取整到秒：向上取整会让 59.7s 显示成「1分0秒」，也会让还没跑完的一轮
 * 提前显示下一档。负数按 0 处理（时钟回拨/服务端时间戳异常时不该出现 `-3秒`）。
 */
export function formatRunDuration(ms: number): DurationPart[] {
  // NaN/Infinity 也按 0：`Math.max(0, NaN)` 仍是 NaN，放任下去会渲染出「NaN秒」
  const safe = Number.isFinite(ms) ? ms : 0;
  const total = Math.max(0, Math.floor(safe / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor(total / 60) % 60;
  const seconds = total % 60;

  const parts: DurationPart[] = [];
  if (hours > 0) {
    parts.push({ text: String(hours), numeric: true }, { text: '小时', numeric: false });
  }
  if (total >= 60) {
    parts.push({ text: String(minutes), numeric: true }, { text: '分', numeric: false });
  }
  parts.push({ text: String(seconds), numeric: true }, { text: '秒', numeric: false });
  return parts;
}

/** 时长片段的纯文本形态（用于 aria-label / 测试断言）。 */
export function formatRunDurationText(ms: number): string {
  return formatRunDuration(ms).map((part) => part.text).join('');
}

/**
 * 一轮结束的原因；与 `message.stopped` / `message.incomplete` / `message.error` 的分支一一对应。
 */
export type TurnEndReason = 'ok' | 'aborted' | 'interrupted' | 'error';

/**
 * 异常结束的状态文案。
 *
 * - `aborted`：用户点了停止（或按 Esc）
 * - `interrupted`：流被截断 / 本轮记录不完整
 * - `error`：本轮失败（上游报错、上下文超限、模型不可用）
 *
 * **正常结束没有文案**：界面上不再为每条完成的回答显示「已完成」。一条回答存在本身
 * 就说明这一轮跑完了，多一个标记只是噪声；耗时挂在消息底部操作栏里。
 */
export function turnStatusLabel(reason: Exclude<TurnEndReason, 'ok'>): string {
  if (reason === 'aborted') return '已停止';
  if (reason === 'interrupted') return '回复不完整（已中断）';
  return '处理失败';
}

/**
 * 把工具名列表聚成 `{kind, count}`，保持首次出现顺序。
 *
 * 一次工具调用算一次；同一类别下的多次调用累加（`bash` 调两次 → commands: 2），
 * 排序在 `processTitle` 里做。
 */
export function summarizeActivities(toolNames: readonly string[]): Array<{
  kind: ActivityKind;
  count: number;
}> {
  const order: ActivityKind[] = [];
  const counter = new Map<ActivityKind, number>();
  for (const toolName of toolNames) {
    const kind = classifyActivity(toolName);
    if (!counter.has(kind)) order.push(kind);
    counter.set(kind, (counter.get(kind) ?? 0) + 1);
  }
  return order.map((kind) => ({ kind, count: counter.get(kind) ?? 0 }));
}

/**
 * 工具行左侧的标题（名词短语，不是句子）。
 *
 * 与组头的「已读取文件」不同：组头是**叙述**（这一轮干了什么），工具行是**标签**
 * （这是一次什么调用）。混用会让同一件事出现两种说法，用户读起来像两条信息。
 *
 * 分两层，与 DSH 的 `TOOL_TITLE_KEYS` + `VARIANT_TITLE_KEYS` 对应：
 * 先查具体工具名的精修标题（`grep` → 搜索文件内容），再退回动作分类的通用标题
 * （`search` → 搜索）。都认不出时给「工具调用」。
 */
const TOOL_TITLES: Record<string, string> = {
  grep: '搜索文件内容',
  glob: '查找文件',
  read_image: '读取图片',
  web_search: '网页搜索',
  web_fetch: '网页获取',
  todo_write: '更新任务清单',
  ask_user_question: '提问',
  ask_user_clarification: '提问',
  create_goal: '创建目标',
  get_goal: '查看目标',
  update_goal: '更新目标',
  // 本仓的工具：`search_knowledge_base` 是本地知识库，不是搜索引擎
  search_knowledge_base: '检索知识库',
  knowledge_search: '检索知识库',
  get_current_time: '查看时间',
  tavily_search: '网页搜索',
};

const ACTIVITY_TITLES: Record<ActivityKind, string> = {
  thinking: '思考',
  read: '读取',
  readImage: '读取图片',
  write: '写入',
  search: '搜索',
  edit: '编辑',
  commands: '运行命令',
  code: '代码',
  webSearch: '网页搜索',
  webFetch: '网页获取',
  time: '查看时间',
  knowledge: '检索知识库',
  subagents: '子智能体',
  plan: '更新计划',
  questions: '提问',
  tools: '工具调用',
};

/** 工具名 → 工具行的标题；认不出时给「工具调用」。 */
export function toolRowTitle(toolName: string): string {
  const name = normalizeToolName(toolName);
  return TOOL_TITLES[name] ?? ACTIVITY_TITLES[classifyActivity(name)] ?? ACTIVITY_TITLES.tools;
}
