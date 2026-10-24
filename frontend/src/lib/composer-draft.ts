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
  /** 可选：不支持 removeItem 的实现用写入空对象代替。 */
  removeItem?: (key: string) => void;
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

/**
 * 清空全部草稿（登出 / 换账号时调用）。
 *
 * 草稿存在 localStorage，而 `clearAllLocalData()` 只清 IndexedDB；不删这个键的话，
 * 登出后按 F12 仍能读到上一个人没发送的敏感文本（L-06）。
 */
export function clearComposerDrafts(storage: DraftStorage | null = getDraftStorage()): void {
  if (!storage) return;
  try {
    if (typeof storage.removeItem === 'function') {
      storage.removeItem(COMPOSER_DRAFT_KEY);
    } else {
      // 老实现/测试替身没有 removeItem：写一个空对象，readDrafts 会当成没有草稿
      storage.setItem(COMPOSER_DRAFT_KEY, '{}');
    }
  } catch (err) {
    console.warn('清空输入框草稿失败:', err);
  }
}

/**
 * 发送成功之后要不要把草稿删掉——**这是「已发送的内容又回到输入框」这个 bug 的判据**。
 *
 * 为什么不能只靠输入框的 `onChange`：发送成功后是 assistant-ui **程序化清空**输入框的，
 * 那条路径不一定经过一次真实的 `input` 事件，于是草稿条目还留在 localStorage 里；
 * 用户切到别的会话再切回来，回填逻辑就把已经发出去的那句话又塞回输入框。
 *
 * 判据是「同一个会话内，从有内容变成空」，两条限定都不能少：
 * - **同会话**：从 A（有内容）切到 B（空）也会呈现「非空 → 空」，但那是换了会话，
 *   把 B 的草稿删掉等于吞掉用户写给 B 的话；
 * - **非空 → 空**：会话刚打开时本来就是空，此时删草稿会把正要回填的那份删掉
 *   （回填与这个判断在同一批 effect 里，顺序不可依赖）。
 */
export function shouldClearDraft(
  previous: { sessionId: string | null; empty: boolean },
  next: { sessionId: string | null; empty: boolean }
): boolean {
  if (!next.sessionId) return false;
  if (previous.sessionId !== next.sessionId) return false;
  return !previous.empty && next.empty;
}
