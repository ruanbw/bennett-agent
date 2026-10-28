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
 *
 * ## 为什么在结尾剥掉系统提示
 *
 * 失败/断流/跨标签冲突时，`chat-store` 会往正文尾部追加一段
 * `> ⚠️ **请求失败**: …`。那是**给用户看的提示**，不是模型的回答——但它是拼在正文里的，
 * 于是「复制回答」会把它一起复制走。同一段提示还会被当成助手历史回放给模型、被导出
 * 写进归档。`types.ts` 里 `FallbackState` 的注释已经把这条不变量写死了，
 * fallback / compaction / usage 都遵守，只有追加提示这一处没有。
 *
 * `stripNotices` 是这条不变量在**读取端**的唯一实现：`toReplayable`（回放给模型）、
 * `messageTextForCopy`（复制）、`export-markdown`（导出）三处都调它，而不是各写一遍
 * 正则——那种散落将来加一种提示形状就会漏掉一处。写入端的形状收敛到
 * `constants.ts:NOTICE_PREFIX`，两边共用同一个常量。
 */

import { NOTICE_PREFIX } from './constants';

export interface CopyablePart {
  type?: string;
  text?: string;
}

/**
 * 结尾的一段提示块：`\n\n> ⚠️ **标签**: 细节` 或 `\n\n> ⚠️ **标签**`。
 *
 * **一段提示 = 一行引用块**。这条形状由写入端保证（`formatNotice` 把 detail 里的
 * 空白折成单个空格），读取端才能按单行精确匹配。写读两边必须成对：任何一边放宽，
 * 要么多行提示永远剥不掉，要么把模型的正文吃掉。
 *
 * 判定刻意写紧，避免误伤模型自己的正文：
 *   - 标签里不允许出现 `*` 或换行（提示标签都是短词）；
 *   - `**` 之后只接受 `: ` 或行尾——模型常写 `> ⚠️ **注意**：这里有风险`（全角冒号），
 *     那不是提示块，必须原样保留；
 *   - 必须以空行开头：正文中间引用同一句话时不会被误截断。
 *
 * **已知限制**：改动前落库的老记录里可能存在多行 detail 的提示，这一条匹配不到，
 * 于是不会被剥掉。不做兼容是刻意的——为它放宽成「一路吃到结尾」会开始吃掉模型写在
 * 提示之后的正文，那是更严重的错误。
 */
const TRAILING_NOTICE = new RegExp(
  // `NOTICE_PREFIX` 本身是 `> ⚠️ **`，不含前导空行，\n\n 由这里补上
  `\\n\\n${NOTICE_PREFIX.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}[^*\\n]+\\*\\*(?:: [^\\n]*)?$`,
);

/**
 * 生成一段系统性提示块（写入端）。
 *
 * 与 `stripNotices` 是同一枚硬币的两面：写入端只允许通过这个函数产生提示，
 * 读取端才可能把每一段都剥掉。`chat-store` 里四处拼字符串的写法因此收敛成一处——
 * 此前它们各自手写 `\n\n> ⚠️ **…`，格式与剥离规则只靠肉眼对齐。
 */
export function formatNotice(label: string, detail?: string): string {
  // detail 折成单行：剥离端按「一段提示 = 一行引用」匹配，写入端若放过换行，那一整段
  // 就永远剥不掉（模型会把多行告警当成自己说过的话读回去）。服务端的错误文案确实
  // 可能带换行，所以这一步不是防御性编程，是这条不变量成立的前提。
  const oneLine = detail?.replace(/\s+/g, ' ').trim();
  const body = oneLine ? `: ${oneLine}` : '';
  return `\n\n${NOTICE_PREFIX}${label}**${body}`;
}

/**
 * 剥掉正文结尾的系统提示块（可能连续多段）。
 *
 * 纯函数：没有任何提示时原样返回；只从**结尾**剥，正文中间的同形引用不动。
 * 连续多段提示（断 3 次就有 3 段）会被循环剥干净。
 */
export function stripNotices(text: string): string {
  let out = text;
  for (;;) {
    const next = out.replace(TRAILING_NOTICE, '');
    if (next === out) return out;
    out = next;
  }
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
  // 先拼再剥：提示可能恰好落在最后一段分片里，「哪些分片非空」不该由它决定
  return stripNotices(texts.join('\n\n')).trim();
}
