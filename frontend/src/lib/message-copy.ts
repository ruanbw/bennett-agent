/**
 * 「复制回答」要复制什么：把消息内容里的文本分片拼起来。
 *
 * 为什么不用 assistant-ui 的 `ActionBarPrimitive.Copy`：它只走 `navigator.clipboard`，
 * 在非安全上下文（局域网 `http://192.168.x.x` 访问）里这个 API 不存在，点了复制
 * 没有任何反应，也没有任何提示。项目里已有带 `execCommand` 回退的
 * `hooks/use-copy-to-clipboard`，这里只负责把「消息 → 纯文本」这一段算清楚。
 *
 * 只取文本分片：思考链默认折叠、工具调用是结构化数据，复制进剪贴板既不可读也可能
 * 让用户把内部草稿贴到别处。空文本返回空串，调用方据此禁用按钮。
 */

export interface CopyablePart {
  type?: string;
  text?: string;
}

export function messageTextForCopy(parts: readonly CopyablePart[] | null | undefined): string {
  if (!parts || parts.length === 0) return '';
  const texts: string[] = [];
  for (const part of parts) {
    if (part?.type !== 'text') continue;
    if (typeof part.text !== 'string') continue;
    if (!part.text) continue;
    texts.push(part.text);
  }
  return texts.join('\n\n').trim();
}
