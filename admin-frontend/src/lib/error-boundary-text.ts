/**
 * 崩溃提示的文案（`components/ErrorBoundary` 用）。
 *
 * 边界此前直接把 `error.message` 贴在页面上。那句话是**给开发者看的**：
 * `Cannot read properties of undefined (reading 'content')`、`Failed to fetch dynamically
 * imported module`、`Minified React error #418`——用户读不懂，也不知道该做什么；
 * 有些还会带出内部字段名。而完全隐藏又让「反馈问题」变成一句「页面白了」。
 *
 * 所以拆成两段：`summary` 是给用户的结论与下一步，`detail` 是给反馈用的技术原文
 * （放在可折叠区域里）。判定只看错误名与消息里的稳定片段，不猜业务语义。
 */

export interface CrashReport {
  /** 面向用户的一句话结论（中文，含下一步）。 */
  summary: string;
  /** 技术原文，供折叠展示与反馈；空串表示拿不到任何信息。 */
  detail: string;
}

/** 分块加载失败：发版后旧页面再拉旧 chunk，或网络中断。刷新通常能解决。 */
const CHUNK_PATTERN = /dynamically imported module|Loading chunk|Importing a module script failed|ChunkLoadError/i;

/** React 压缩后的错误号（`Minified React error #418` 之类）。 */
const MINIFIED_REACT_PATTERN = /Minified React error #\d+/i;

/** 读到了不存在的属性/调用：多半是数据形状与组件预期不符。 */
const UNDEFINED_ACCESS_PATTERN = /Cannot read propert|of undefined|of null|is not a function/i;

const GENERIC_SUMMARY = '界面遇到未预期的错误，可以重试；如果反复出现，请刷新页面。';
const CHUNK_SUMMARY = '页面资源加载失败，可能是应用刚更新过。刷新页面即可载入新版本。';
const DATA_SUMMARY = '界面拿到的数据与预期不一致，这一块无法显示。重试通常能恢复；若一直失败，请刷新页面。';

function messageOf(error: unknown): string {
  if (error instanceof Error) return error.message;
  if (typeof error === 'string') return error;
  if (error && typeof error === 'object' && 'message' in error) {
    const message = (error as { message?: unknown }).message;
    if (typeof message === 'string') return message;
  }
  return '';
}

function nameOf(error: unknown): string {
  if (error instanceof Error) return error.name;
  return '';
}

/**
 * 把捕获到的异常翻译成「用户能懂的一句话 + 技术原文」。
 *
 * 顺序有讲究：分块加载失败排在最前——它的 message 里通常也含 `Failed to fetch`，
 * 而「刷新载入新版本」是唯一正确的动作；先判数据形状会把它归错类。
 */
export function describeCrash(error: unknown): CrashReport {
  const message = messageOf(error);
  const name = nameOf(error);
  // 没有消息时不给 detail：`new Error('')` 的 name 是 'Error'，把光秃秃的 "Error"
  // 当作「错误详情」展示出来，除了占位没有任何信息量。
  const detail = message ? (name ? `${name}: ${message}` : message) : '';

  if (CHUNK_PATTERN.test(message) || name === 'ChunkLoadError') {
    return { summary: CHUNK_SUMMARY, detail };
  }
  if (UNDEFINED_ACCESS_PATTERN.test(message)) {
    return { summary: DATA_SUMMARY, detail };
  }
  if (MINIFIED_REACT_PATTERN.test(message)) {
    // 压缩后的错误号对用户无意义，但它是反馈时唯一能定位的线索，保留在 detail 里
    return { summary: GENERIC_SUMMARY, detail };
  }
  return { summary: GENERIC_SUMMARY, detail };
}
