/**
 * 同步状态的入口按钮与详情面板。
 *
 * 设计取舍：**成功状态也要有一个常驻入口**。
 *
 * 旧实现只在「有待推送」或「失败」时渲染一个只读 `<span>`：一切正常时什么都不显示，
 * 用户无法确认对话到底有没有备份成功；失败时只有一句 `title`，被服务端拒收的行则
 * 从来不出现在界面上（它们永远停在 error，自动重试也不会碰）。同步这类功能最糟的
 * 失败方式是**静默**——用户以为存上了，换设备才发现没有。
 *
 * 所以这里把角标改成真正的按钮：图标常驻（正常时很淡），点击打开面板看状态、时间、
 * 待上传与被拒数量，并给出「立即同步 / 重新上传」两个恢复入口。
 */

import { useCallback, useEffect, useState } from 'react';
import {
  Cloud,
  CloudAlert,
  CloudCheck,
  CloudOff,
  CloudUpload,
  Loader2,
  type LucideIcon,
} from 'lucide-react';

import { Button } from './ui/button';
import {
  Popover,
  PopoverContent,
  PopoverDescription,
  PopoverTitle,
  PopoverTrigger,
} from './ui/popover';
import { useSyncStatus } from '../hooks/use-sync-status';
import { retryNow, retryRejected } from '../lib/sync/engine';
import {
  describeSyncStatus,
  formatAbsoluteTime,
  formatRelativeTime,
  type SyncTone,
} from '../lib/sync/status-view';
import { cn } from '../lib/utils';

interface ToneStyle {
  Icon: LucideIcon;
  /** 触发器（图标按钮）的配色。 */
  trigger: string;
  /** 面板顶部圆形图标的底色。 */
  bubble: string;
}

const TONE_STYLES: Record<SyncTone, ToneStyle> = {
  idle: {
    Icon: Cloud,
    trigger: 'text-muted-foreground/60 hover:text-foreground',
    bubble: 'bg-muted text-muted-foreground',
  },
  ok: {
    Icon: CloudCheck,
    trigger: 'text-muted-foreground/60 hover:text-foreground',
    bubble: 'bg-muted text-muted-foreground',
  },
  busy: {
    Icon: CloudUpload,
    trigger: 'text-muted-foreground hover:text-foreground',
    bubble: 'bg-muted text-muted-foreground',
  },
  warning: {
    Icon: CloudAlert,
    trigger: 'text-amber-600 hover:bg-amber-500/10 dark:text-amber-400',
    bubble: 'bg-amber-500/10 text-amber-600 dark:text-amber-400',
  },
  error: {
    Icon: CloudOff,
    trigger: 'text-destructive hover:bg-destructive/10',
    bubble: 'bg-destructive/10 text-destructive',
  },
};

/** 三位数以上别把按钮撑开。 */
function formatBadge(count: number): string {
  return count > 99 ? '99+' : String(count);
}

export function SyncStatusButton() {
  const status = useSyncStatus();
  const [open, setOpen] = useState(false);
  // 面板开着时让「上次同步：刚刚」自己走起来，而不是停在打开那一刻。
  const [now, setNow] = useState(() => Date.now());
  const [requeueing, setRequeueing] = useState(false);

  useEffect(() => {
    if (!open) return undefined;
    const timer = setInterval(() => setNow(Date.now()), 20_000);
    return () => clearInterval(timer);
  }, [open]);

  const view = describeSyncStatus(status);
  const { Icon, trigger, bubble } = TONE_STYLES[view.tone];
  const label = `${view.headline}，查看同步详情`;

  const handleRetryRejected = useCallback(async () => {
    setRequeueing(true);
    try {
      await retryRejected();
    } catch (err) {
      // 面板会通过引擎状态反映失败；这里只保证不产生 unhandled rejection
      console.error('重新上传被拒内容失败:', err);
    } finally {
      setRequeueing(false);
    }
  }, []);

  return (
    <Popover
      open={open}
      onOpenChange={(next) => {
        setOpen(next);
        // 每次打开都校准一次时间：关闭期间「刚刚」会变成「5 分钟前」，
        // 但没必要在后台每 20 秒渲染一次。
        if (next) setNow(Date.now());
      }}
    >
      <PopoverTrigger
        aria-label={label}
        title={label}
        className={cn(
          'flex h-8 items-center justify-center gap-1 rounded-lg transition cursor-pointer outline-none focus-visible:ring-1 focus-visible:ring-ring',
          view.badgeCount > 0 ? 'min-w-8 px-1.5' : 'size-8',
          trigger,
        )}
      >
        <Icon
          size={16}
          aria-hidden="true"
          className={cn(view.tone === 'busy' && 'animate-pulse')}
        />
        {view.badgeCount > 0 && (
          <span className="text-[10px] font-medium tabular-nums" aria-hidden="true">
            {formatBadge(view.badgeCount)}
          </span>
        )}
      </PopoverTrigger>

      <PopoverContent side="bottom" align="end" className="w-72">
        <div className="flex items-start gap-2.5">
          <span
            className={cn(
              'mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-full',
              bubble,
            )}
          >
            <Icon size={15} aria-hidden="true" />
          </span>
          <div className="min-w-0">
            <PopoverTitle className="text-[13px] leading-5 font-medium">
              {view.headline}
            </PopoverTitle>
            <PopoverDescription className="mt-0.5 text-[11px] leading-relaxed text-muted-foreground">
              {view.detail}
            </PopoverDescription>
          </div>
        </div>

        <dl className="mt-3 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1.5 border-t border-border/60 pt-2.5 text-[11px]">
          <dt className="text-muted-foreground">上次同步</dt>
          <dd
            className="text-right tabular-nums text-foreground/90"
            title={status.lastSyncedAt === null ? undefined : formatAbsoluteTime(status.lastSyncedAt)}
          >
            {formatRelativeTime(status.lastSyncedAt, now)}
          </dd>

          <dt className="text-muted-foreground">待上传</dt>
          <dd className="text-right tabular-nums text-foreground/90">
            {status.pending > 0 ? `${status.pending} 条` : '无'}
          </dd>

          {status.rejected > 0 && (
            <>
              <dt className="text-amber-600 dark:text-amber-400">无法同步</dt>
              <dd className="text-right tabular-nums text-amber-600 dark:text-amber-400">
                {status.rejected} 条
              </dd>
            </>
          )}
        </dl>

        <div className="mt-3 flex items-center gap-2">
          {status.rejected > 0 && (
            <Button
              type="button"
              size="sm"
              variant="outline"
              disabled={requeueing}
              onClick={() => void handleRetryRejected()}
              className="text-[11px]"
            >
              {requeueing && <Loader2 className="size-3 animate-spin" />}
              重新上传
            </Button>
          )}
          <Button
            type="button"
            size="sm"
            variant={status.rejected > 0 ? 'ghost' : 'outline'}
            disabled={!status.running || requeueing}
            onClick={() => retryNow()}
            className={cn('text-[11px]', status.rejected > 0 ? 'ml-auto' : '')}
          >
            立即同步
          </Button>
        </div>

        <p className="mt-2.5 text-[10.5px] leading-relaxed text-muted-foreground/80">
          同步在后台自动进行；在其他设备登录同一账号即可看到这些对话。
        </p>
      </PopoverContent>
    </Popover>
  );
}
