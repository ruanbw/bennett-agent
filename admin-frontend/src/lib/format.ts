/** 时间戳 → 本地短格式；空值显示 —。放在 lib 层：纯函数，不与组件同文件（fast-refresh 约束）。 */
export function formatDateTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  return date.toLocaleString('zh-CN', { dateStyle: 'short', timeStyle: 'short' });
}
