import { AlertCircle, RefreshCw } from 'lucide-react';

import { formatStorageUsage } from '../lib/local-stats';
import type { LocalDataStats } from '../lib/local-stats';

/**
 * 设置页里的「本机数据」区块。
 *
 * 单独成一个组件是为了能被服务端渲染测试直接喂状态：三种状态（读到了 / 还在读 /
 * 读失败）的文案差别就是这块的全部价值，绑在 hook 里就只能靠交互测试去验证。
 *
 * 读失败时必须说明「读不出来 != 数据没了」：这个应用的数据默认只存在本机，
 * 一行 dash 会让用户以为会话被清空了。
 */
export function LocalDataStatsSection({
  stats,
  failed,
  onRetry,
}: {
  stats: LocalDataStats | null;
  failed: boolean;
  onRetry?: () => void;
}) {
  return (
    <div className="rounded-lg border border-border/70 bg-muted/40 p-3 space-y-1.5 text-xs">
      <div className="flex justify-between items-center">
        <span className="text-muted-foreground">会话</span>
        <span className="tabular-nums text-foreground">{stats ? `${stats.sessions} 个` : '—'}</span>
      </div>
      <div className="flex justify-between items-center">
        <span className="text-muted-foreground">消息</span>
        <span className="tabular-nums text-foreground">
          {stats ? `${stats.messages} 条` : '—'}
        </span>
      </div>
      <div className="flex justify-between items-center">
        <span className="text-muted-foreground">存储占用</span>
        <span
          className="tabular-nums text-foreground"
          title="浏览器对整个站点的估算值，包含缓存等其他数据；括号内为估算配额占比"
        >
          {stats ? formatStorageUsage(stats.usageBytes, stats.quotaBytes) : '—'}
        </span>
      </div>

      {failed && (
        <div
          role="alert"
          className="mt-1 flex flex-col gap-1 rounded-md border border-amber-500/40 bg-amber-500/10 px-2 py-1.5 text-[10.5px] leading-relaxed text-amber-700 dark:text-amber-400"
        >
          <span className="flex items-center gap-1">
            <AlertCircle className="size-3 shrink-0" aria-hidden="true" />
            读不出本机数据统计（浏览器可能禁止了本站点的本地存储）。这只影响这里的计数，
            不代表会话内容已经丢失。
          </span>
          {onRetry && (
            <button
              type="button"
              onClick={onRetry}
              className="inline-flex w-fit cursor-pointer items-center gap-1 rounded-md border border-amber-500/40 bg-background/70 px-2 py-0.5 font-medium text-foreground hover:bg-background focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring/60"
            >
              <RefreshCw className="size-2.5" aria-hidden="true" />
              重新读取
            </button>
          )}
        </div>
      )}

      <p className="text-[10.5px] leading-relaxed text-muted-foreground/80">
        会话默认保存在本机浏览器；登录同一账号时后台同步到服务器。存储占用是浏览器对本站点的估算值，括号内为估算配额占比。
      </p>
    </div>
  );
}
