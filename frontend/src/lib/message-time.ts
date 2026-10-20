/**
 * 消息时间的显示格式。
 *
 * 目标只有一个：**不点开也知道这条是什么时候说的**，同时不把整块时间戳塞满气泡。
 *
 * - 今天 → `14:05`
 * - 今年其它日期 → `10-20 14:05`
 * - 跨年 → `2025-12-31 14:05`
 *
 * 规则用本地日历日判断（不是 24 小时差）：凌晨 00:10 看昨天的 23:50，应该显示日期而不是
 * 「00:20 前」这类相对时间——聊天里「哪一天」比「几小时前」更重要。
 *
 * 完整时间（含秒）放 `title`：精确到秒只在排查问题时有用，平时不该占视觉空间。
 */

function pad2(value: number): string {
  return String(value).padStart(2, '0');
}

function isValidDate(date: Date | null | undefined): date is Date {
  return date instanceof Date && !Number.isNaN(date.getTime());
}

/** `HH:mm` / `MM-DD HH:mm` / `YYYY-MM-DD HH:mm`；非法时间返回空串（调用方据此不渲染）。 */
export function formatMessageTime(date: Date | null | undefined, now: Date = new Date()): string {
  if (!isValidDate(date)) return '';

  const time = `${pad2(date.getHours())}:${pad2(date.getMinutes())}`;
  const sameDay =
    date.getFullYear() === now.getFullYear() &&
    date.getMonth() === now.getMonth() &&
    date.getDate() === now.getDate();
  if (sameDay) return time;

  const monthDay = `${pad2(date.getMonth() + 1)}-${pad2(date.getDate())}`;
  if (date.getFullYear() === now.getFullYear()) return `${monthDay} ${time}`;

  return `${date.getFullYear()}-${monthDay} ${time}`;
}

/** 悬浮提示用：`YYYY-MM-DD HH:mm:ss`。非法时间返回空串。 */
export function formatMessageTimeFull(date: Date | null | undefined): string {
  if (!isValidDate(date)) return '';
  return `${date.getFullYear()}-${pad2(date.getMonth() + 1)}-${pad2(date.getDate())} ${pad2(
    date.getHours(),
  )}:${pad2(date.getMinutes())}:${pad2(date.getSeconds())}`;
}
