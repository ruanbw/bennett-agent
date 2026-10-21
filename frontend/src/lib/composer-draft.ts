/**
 * 输入框草稿：按会话保存在 localStorage 里。
 *
 * 为什么值得有：一半写好的问题，在「切去另一个会话看一眼」或「手滑刷新」之后就没有了。
 * 对话内容本身存在 IndexedDB 里，唯独最贵的那部分（用户正在组织的那句话）此前只活在
 * React 状态里。
 *
 * 几条边界：
 *
 * - **按会话存**：切走再回来应该看到自己那份，而不是把 A 会话的草稿带进 B 会话。
 * - **只存有内容的**：空串等于删除条目（发送成功后输入框会清空，草稿随之消失）。
 * - **上限**：单条超过 `MAX_DRAFT_CHARS` 不存（localStorage 配额有限，一次粘贴整篇文档
 *   不该把其他草稿挤掉）；最多保留 `MAX_DRAFTS` 个会话，按最后编辑时间淘汰最旧的。
 * - **存储不可用就当没有**：Safari 阻止 Cookie、sandbox iframe 下访问 localStorage 会抛
 *   SecurityError——草稿是增强，不能因此让输入框打不了字。
 */

/** 存储键：所有会话的草稿放在一个 JSON 里，读一次就够（不必遍历 localStorage）。 */
export const COMPOSER_DRAFT_KEY = 'composer_drafts';
/** 单条草稿的字符上限；超过就不存（避免一次粘贴把配额吃满）。 */
export const MAX_DRAFT_CHARS = 8_000;
/** 最多保留多少个会话的草稿。 */
export const MAX_DRAFTS = 20;

export interface DraftEntry {
  text: string;
  /** 最后编辑时间（毫秒），用来淘汰最旧的草稿。 */
  at: number;
}

/** localStorage 的最小接口：只用到 getItem/setItem，方便单测注入假实现。 */
export interface DraftStorage {
  getItem: (key: string) => string | null;
  setItem: (key: string, value: string) => void;
}

/** 取 localStorage；访问本身抛异常时返回 null（与 `theme-context.getThemeStorage` 同口径）。 */
export function getDraftStorage(): DraftStorage | null {
  if (typeof window === 'undefined') return null;
  try {
    return window.localStorage;
  } catch (err) {
    console.warn('localStorage 不可用，输入框草稿不会被保存:', err);
    return null;
  }
}

/** 读出全部草稿；内容坏了（手改过、旧版本格式）就当空的，绝不抛。 */
export function readDrafts(storage: DraftStorage | null = getDraftStorage()): Record<string, DraftEntry> {
  if (!storage) return {};
  let raw: string | null = null;
  try {
    raw = storage.getItem(COMPOSER_DRAFT_KEY);
  } catch (err) {
    console.warn('读取输入框草稿失败:', err);
    return {};
  }
  if (!raw) return {};

  try {
    const parsed: unknown = JSON.parse(raw);
    if (!parsed || typeof parsed !== 'object') return {};
    const drafts: Record<string, DraftEntry> = {};
    for (const [sessionId, value] of Object.entries(parsed as Record<string, unknown>)) {
      if (!sessionId || !value || typeof value !== 'object') continue;
      const { text, at } = value as Partial<DraftEntry>;
      if (typeof text !== 'string' || !text) continue;
      drafts[sessionId] = { text, at: typeof at === 'number' && Number.isFinite(at) ? at : 0 };
    }
    return drafts;
  } catch {
    // JSON 坏了：当作没有草稿。不尝试修复——写回时自然会覆盖掉
    return {};
  }
}

/** 某个会话的草稿文本；没有返回空串。 */
export function readDraft(
  storage: DraftStorage | null,
  sessionId: string | null | undefined
): string {
  if (!sessionId) return '';
  return readDrafts(storage)[sessionId]?.text ?? '';
}

/**
 * 写入（或清除）某个会话的草稿。
 *
 * 空文本 / 超长文本都按「不存」处理，并顺手删掉旧条目——用户把它清空了，草稿就该消失。
 */
export function writeDraft(
  storage: DraftStorage | null,
  sessionId: string | null | undefined,
  text: string,
  now: number = Date.now()
): void {
  if (!storage || !sessionId) return;

  const drafts = readDrafts(storage);
  const trimmed = text.trim();

  if (!trimmed || Array.from(text).length > MAX_DRAFT_CHARS) {
    if (!(sessionId in drafts)) return;
    delete drafts[sessionId];
  } else {
    drafts[sessionId] = { text, at: now };
  }

  // 淘汰最旧的：条目数只受会话数影响，上限很小，一次排序足够
  const entries = Object.entries(drafts);
  if (entries.length > MAX_DRAFTS) {
    entries.sort((a, b) => a[1].at - b[1].at);
    for (const [staleId] of entries.slice(0, entries.length - MAX_DRAFTS)) {
      delete drafts[staleId];
    }
  }

  try {
    storage.setItem(COMPOSER_DRAFT_KEY, JSON.stringify(drafts));
  } catch (err) {
    // 配额满 / 隐私模式：草稿存不下就算了，不能影响用户继续打字
    console.warn('保存输入框草稿失败:', err);
  }
}
