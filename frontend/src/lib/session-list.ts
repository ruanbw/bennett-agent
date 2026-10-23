/**
 * 会话列表的纯逻辑：排序、重命名/置顶要写库的字段、多选与搜索过滤的交集。
 *
 * 放在 lib 而不是组件/hook 里：这几条规则最容易悄悄出错（一次无变化的保存把旧会话
 * 顶到最前、批量操作牵连被搜索隐藏的选中项），而 vitest 的 node 环境跑不了组件与
 * IndexedDB。规则本身抽成纯函数后就能直接回归。
 */

import type { SessionRecord } from '../types';

/** 排序：置顶在前；同组内按最近活动（updatedAt）倒序。不原地修改入参。 */
export function sortSessions(sessions: readonly SessionRecord[]): SessionRecord[] {
  return [...sessions].sort((a, b) => {
    const pinA = a.isPinned ? 1 : 0;
    const pinB = b.isPinned ? 1 : 0;
    if (pinA !== pinB) return pinB - pinA;
    return (b.updatedAt || 0) - (a.updatedAt || 0);
  });
}

/**
 * 会话标题的长度上限（按 Unicode 码点计）。
 *
 * 用码点而不是 UTF-16 长度：`"😀".length === 2`，按 `.slice()` 截断会把代理对劈开，
 * 留下一个孤立代理项（渲染成 �）。上限值与产品语义一致（100 个字符，不是 100 个
 * 存储单元）。
 */
export const TITLE_MAX_LENGTH = 100;

/** 标题的「字数」：按码点计，与 `clampTitle` 的口径一致。 */
export function titleLength(value: string): number {
  return Array.from(value).length;
}

/**
 * 把超长标题截到上限。
 *
 * 不返回布尔之外的信息：调用方只需要「要不要提示用户被截断了」。用码点切片，
 * 不会劈开 emoji / 组合字符的代理对（组合字符仍可能被切开，但不会产生非法字符串）。
 */
export function clampTitle(value: string): { title: string; truncated: boolean } {
  const chars = Array.from(value);
  if (chars.length <= TITLE_MAX_LENGTH) return { title: value, truncated: false };
  return { title: chars.slice(0, TITLE_MAX_LENGTH).join(''), truncated: true };
}

/**
 * 重命名要写库的字段；标题没有任何变化时返回 null（调用方跳过写库）。
 *
 * 原实现无条件 bump updatedAt：点开重命名、原样保存也会把一条旧会话顶到列表最前。
 * 传 undefined 表示当前列表里找不到这条记录（例如列表还没加载完），此时无从比较，
 * 照写——静默丢弃重命名比多写一次更糟。
 */
export function titleChange(
  session: Pick<SessionRecord, 'title'> | undefined,
  nextTitle: string,
  now: number,
): { title: string; updatedAt: number } | null {
  if (session && session.title === nextTitle) return null;
  return { title: nextTitle, updatedAt: now };
}

/**
 * 置顶/取消置顶要写库的字段（单条与批量共用，避免两种入口语义漂移）。
 *
 * 刻意不 bump updatedAt：updatedAt 的含义是「最近活动」，改置顶不是活动。bump 会让
 * 「取消置顶」把一条旧会话顶到未置顶区最前、「置顶」又让它排在置顶区最前——顺序由
 * 点按钮的先后决定。不 bump 时排序稳定：置顶只换分组，组内仍按最近活动排。
 */
export function pinChange(pinned: boolean): { isPinned: number } {
  return { isPinned: pinned ? 1 : 0 };
}

/**
 * 一个会话是否只是「开了没聊」的空占位。
 *
 * 两条用户可见的规则都建立在它上面：当前会话还是空占位时，「开启新对话」不再叠一个
 * （否则列表里攒出一串同名「新对话」）；切到历史会话时把空占位收走（它没有任何内容，
 * 不值得留作占位）。
 *
 * 三个排除项都是「已经表达了内容意图」的信号：
 *   - 墓碑：已经删掉的行不该再被判定一次；
 *   - 正在生成：流式回答先进内存、后落库，只数消息条数会把它误判为空；
 *   - 已置顶：置顶是显式的「留着」动作，删掉它与用户意图相反。
 */
export function isBlankDraft(
  session: Pick<SessionRecord, 'deleted' | 'isPinned'> | undefined,
  messageCount: number,
  runActive: boolean,
): boolean {
  if (!session || session.deleted) return false;
  if (runActive) return false;
  if (session.isPinned) return false;
  return messageCount === 0;
}

/**
 * 选中集合与「当前可见（搜索过滤后）」集合的交集。
 *
 * 批量操作只作用于这个集合：搜索把某些已选会话藏起来时不能被顺带删除，
 * 确认弹窗里的数量也必须等于实际删除集合。
 */
export function visibleSelection(
  selectedIds: ReadonlySet<string>,
  visibleIds: readonly string[],
): Set<string> {
  const visible = new Set<string>();
  for (const id of visibleIds) {
    if (selectedIds.has(id)) visible.add(id);
  }
  return visible;
}

/**
 * 单个会话的删除确认文案。
 *
 * 确认框的任务不是重复「无法恢复」，而是让用户知道**具体会失去什么**：消息条数、
 * 是否正在生成。正在生成时删除会直接中止回答，这一条必须写出来。
 */
export function deleteSessionDescription(
  title: string,
  messageCount: number,
  running: boolean,
): string {
  const name = title.trim() || '新对话';
  const messages = messageCount > 0 ? `及其 ${messageCount} 条消息` : '';
  const stop = running ? '该会话正在生成回答，删除会立即停止回答。' : '';
  return `「${name}」${messages}将被删除，且无法恢复。${stop}`;
}

/** 批量删除的确认文案：会话数 + 总消息数 + 其中正在生成的会话数。 */
export function batchDeleteDescription(
  sessionCount: number,
  messageCount: number,
  runningCount: number,
): string {
  const messages = messageCount > 0 ? `（共 ${messageCount} 条消息）` : '';
  const stop = runningCount > 0 ? `其中 ${runningCount} 个正在生成回答，删除会立即停止。` : '';
  return `选中的 ${sessionCount} 个会话${messages}将被删除，且无法恢复。${stop}`;
}
