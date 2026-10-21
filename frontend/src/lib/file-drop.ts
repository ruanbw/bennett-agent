/**
 * 窗口级「文件被拖到界面其它地方」的兜底。
 *
 * 输入框自己处理了拖拽与粘贴（见 `thread.aui.tsx` 的 onDrop / onPaste）。但把文件拖到
 * **输入框以外**的任意位置（消息列表、侧边栏、空白处）时，浏览器默认行为是**导航到那个
 * 文件**：整个单页应用被替换掉，正在生成的回答、未发送的草稿全部丢失，用户还会以为
 * 「页面崩了」——这比没有附件功能糟得多。
 *
 * 所以窗口层要 `preventDefault()`。但只在「拖的确实是文件」时拦：文本、选区、链接的
 * 拖放有各自的语义（在输入框里拖动选中的文字），拦掉会破坏这些操作。
 *
 * 判定与文案是纯函数，能在 node 环境直接测。
 */

/** 拖拽/粘贴载荷里是否含文件。`types` 是 `DataTransfer.types`。 */
export function dragHasFiles(types: readonly string[] | undefined | null): boolean {
  if (!types) return false;
  return types.includes('Files');
}

/**
 * 事件目标是否在输入框区域内。
 *
 * 区域内的拖放由 Composer 自己处理（它会给出更具体的提示，且未来可能真的接住文件），
 * 窗口兜底不该抢在它前面重复提示。
 */
export const COMPOSER_REGION_SELECTOR = '[data-slot="aui_composer-root"]';

export function isInsideComposerRegion(target: EventTarget | null): boolean {
  if (!target || typeof (target as Element).closest !== 'function') return false;
  return Boolean((target as Element).closest(COMPOSER_REGION_SELECTOR));
}

/**
 * 拖到界面其它地方时的提示。
 *
 * 说清两件事：文件没被接受（否则用户以为加上了），以及去哪里能用它——当前版本只接受
 * 文本，粘贴文本或直接描述需求。
 */
export function foreignFileDropMessage(count: number): string {
  const files = count > 1 ? `${count} 个文件` : '文件';
  return `附件暂不支持：拖入的${files}没有添加，后端目前只接受文本。`;
}

/**
 * 窗口级 drop 是否应该被拦下（纯判定，便于单测）。
 *
 * 已被别人 `preventDefault()` 的事件不再重复处理：输入框先处理过时，它会自己提示。
 */
export function shouldBlockWindowFileDrop(event: {
  defaultPrevented: boolean;
  types: readonly string[] | null | undefined;
  target: EventTarget | null;
}): boolean {
  if (event.defaultPrevented) return false;
  if (!dragHasFiles(event.types)) return false;
  return !isInsideComposerRegion(event.target);
}
