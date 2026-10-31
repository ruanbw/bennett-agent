/**
 * 插入排队的纯数据层：可见性判定与预览文本裁剪。
 *
 * 单独放在 `lib` 而不是组件文件里：同时被组件（渲染）与测试（node 环境的 vitest）
 * 使用——留在组件文件里会让 Vite 的 Fast Refresh 退化成整页刷新（同一模块既导出
 * 组件又导出纯函数）。
 */

/** 排队条可见性：队列非空就显示——与是否正在生成无关（空闲连发也进队）。 */
export function hasQueuedMessages(queue: ReadonlyArray<unknown>): boolean {
  return queue.length > 0;
}

/** 单条排队消息的预览文本：压平空白、超长截断（默认 60 字符，省略号占 1 字）。 */
export function queueItemPreview(text: string, maxChars = 60): string {
  const normalized = text.replace(/\s+/g, ' ').trim();
  if (normalized.length <= maxChars) return normalized;
  return `${normalized.slice(0, Math.max(0, maxChars - 1))}…`;
}
