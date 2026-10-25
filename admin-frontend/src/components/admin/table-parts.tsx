/**
 * 页面骨架与表格的共享样式。
 *
 * 两个页面（提供商 / 用户）共用同一套「页头 + 筛选区 + 数据表格」的形态：
 * 页头在这里统一（标题、说明、右侧主操作），表格的类名 vocabulary 也收敛成常量，
 * 改一处三个表同时变——各页面只写自己的列与行。
 */

import type { ReactNode } from 'react';
import { Loader2 } from 'lucide-react';

/** 状态 / 角色徽标。 */
export interface BadgeProps {
  tone: 'ok' | 'danger' | 'primary' | 'muted';
  children: ReactNode;
}

const badgeToneClass: Record<BadgeProps['tone'], string> = {
  ok: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
  danger: 'bg-destructive/10 text-destructive',
  primary: 'bg-primary/10 text-primary',
  muted: 'bg-muted text-muted-foreground',
};

export function Badge({ tone, children }: BadgeProps) {
  return (
    <span className={`inline-block rounded-full px-1.5 py-0.5 text-[10px] font-medium ${badgeToneClass[tone]}`}>
      {children}
    </span>
  );
}

export interface PageShellProps {
  title: string;
  /** 页头标题下的说明文字。 */
  description: ReactNode;
  /** 页头右侧的主操作（如「新增提供商」）。 */
  actions?: ReactNode;
  children: ReactNode;
}

export function PageShell({ title, description, actions, children }: PageShellProps) {
  return (
    <section className="space-y-4" aria-labelledby="page-heading">
      <div className="flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 id="page-heading" className="text-base font-semibold tracking-tight text-foreground">
            {title}
          </h2>
          <p className="mt-1 max-w-3xl text-xs leading-relaxed text-muted-foreground">{description}</p>
        </div>
        {actions ? <div className="flex items-center gap-1.5">{actions}</div> : null}
      </div>
      {children}
    </section>
  );
}

/** 筛选表单区：浅底卡片，横向排布各筛选控件。 */
export function Toolbar({ children, 'aria-label': ariaLabel }: { children: ReactNode; 'aria-label'?: string }) {
  return (
    <div
      role="search"
      aria-label={ariaLabel}
      className="flex flex-wrap items-center gap-2 rounded-xl border border-border/70 bg-card/50 px-3 py-2.5"
    >
      {children}
    </div>
  );
}

/** 表格容器：窄屏横向滚动，避免挤压出换行灾难。 */
export function TableWrap({ children, minWidth = '48rem' }: { children: ReactNode; minWidth?: string }) {
  return (
    <div className="overflow-hidden rounded-xl border border-border/70 bg-card/50">
      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-left" style={{ minWidth }}>
          {children}
        </table>
      </div>
    </div>
  );
}

export const TH_CLASS =
  'border-b border-border/70 bg-muted/40 px-3 py-2.5 text-xs font-medium whitespace-nowrap text-muted-foreground';

export const TD_CLASS = 'border-b border-border/70 px-3 py-2.5 align-middle text-sm text-foreground';

/** 表格空态 / 加载态的一整行占位。 */
export function TableStatusRow({
  colSpan,
  loading,
  text,
}: {
  colSpan: number;
  loading?: boolean;
  text: string;
}) {
  return (
    <tr>
      <td colSpan={colSpan} className="px-3 py-10 text-center text-xs text-muted-foreground">
        <span role={loading ? 'status' : undefined} className="inline-flex items-center gap-2">
          {loading ? <Loader2 className="size-4 animate-spin" aria-hidden="true" /> : null}
          {text}
        </span>
      </td>
    </tr>
  );
}
