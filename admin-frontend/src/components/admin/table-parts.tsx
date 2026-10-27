/**
 * 列表页的共享骨架：页头 + 工具栏 + 卡片包住的表格 + 页脚分页。
 *
 * 经典后台的列表页只有一种形状，三个页面（概览 / 提供商 / 用户）共用：
 *
 *   ┌ 页头：标题 + 一句话说明                      [主操作] ┐
 *   ├ 工具栏：筛选控件 + 批量操作（仅选中时出现）  + 计数   ┤
 *   ├ 卡片：表头 / 行 / 空态                                 ┤
 *   └ 页脚：区间计数（左）                     分页（右）   ┘
 *
 * 分页控件与页码窗口由 `react-paginate` 的纯计算部分承担？不需要——这里做的是**排版**
 * 不是逻辑：页码序列是一个 10 行的纯函数（`pageWindow`，有单测），套一个库反而多一层
 * 要跟样式对齐的 DOM。
 *
 * 这里刻意**不**抽象成 `<DataTable columns=...>`：三个页面的列、行操作、状态处理差别
 * 很大（提供商还有可展开的子表），抽象出来只会多一层配置要维护。
 */

import type { ReactNode } from 'react';
import { ChevronLeft, ChevronRight, Inbox, Loader2 } from 'lucide-react';

import { Button } from '@/components/ui/button';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { cn } from '@/lib/utils';

/** 状态 / 角色徽标。 */
export interface BadgeProps {
  tone: 'ok' | 'danger' | 'warn' | 'primary' | 'muted';
  children: ReactNode;
}

const badgeToneClass: Record<BadgeProps['tone'], string> = {
  ok: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
  danger: 'bg-destructive/10 text-destructive',
  warn: 'bg-amber-500/10 text-amber-600 dark:text-amber-400',
  primary: 'bg-primary/10 text-primary',
  muted: 'bg-muted text-muted-foreground',
};

export function Badge({ tone, children }: BadgeProps) {
  return (
    <span
      className={cn(
        'inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] font-medium whitespace-nowrap',
        badgeToneClass[tone],
      )}
    >
      {children}
    </span>
  );
}

export interface PageShellProps {
  title: string;
  /** 标题下的说明：写清「这一页能做什么」，不是功能清单。 */
  description: ReactNode;
  /** 页头右侧的主操作（如「新增提供商」）。 */
  actions?: ReactNode;
  children: ReactNode;
}

export function PageShell({ title, description, actions, children }: PageShellProps) {
  return (
    <section className="space-y-4" aria-labelledby="page-heading">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h2 id="page-heading" className="text-lg font-semibold tracking-tight text-foreground">
            {title}
          </h2>
          <p className="mt-1 max-w-3xl text-xs leading-relaxed text-muted-foreground">{description}</p>
        </div>
        {actions ? <div className="flex shrink-0 items-center gap-2">{actions}</div> : null}
      </div>
      {children}
    </section>
  );
}

/** 筛选 / 操作工具栏：卡片之上的一条横带。 */
export function Toolbar({
  children,
  'aria-label': ariaLabel,
}: {
  children: ReactNode;
  'aria-label'?: string;
}) {
  return (
    <div
      role="search"
      aria-label={ariaLabel}
      className="flex flex-wrap items-center gap-2 rounded-lg border border-border/70 bg-card px-3 py-2.5"
    >
      {children}
    </div>
  );
}

/**
 * 表格卡片：可选顶部工具栏 + 表格（窄屏横向滚动）+ 可选页脚。
 *
 * 卡片自己 `overflow-hidden`，表格的滚动容器再包一层——否则圆角会被滚动容器裁掉。
 */
export function TableWrap({
  children,
  minWidth = '48rem',
  toolbar,
  footer,
}: {
  children: ReactNode;
  minWidth?: string;
  /** 与表头同宽、视觉上属于同一张卡片的工具栏（批量操作就放这里）。 */
  toolbar?: ReactNode;
  /** 卡片页脚（区间计数 / 分页）。 */
  footer?: ReactNode;
}) {
  return (
    <div className="overflow-hidden rounded-lg border border-border/70 bg-card">
      {toolbar}
      <div className="overflow-x-auto">
        <table className="w-full border-collapse text-left" style={{ minWidth }}>
          {children}
        </table>
      </div>
      {footer}
    </div>
  );
}

export const TH_CLASS =
  'border-b border-border/70 bg-muted/40 px-3 py-2 text-xs font-medium whitespace-nowrap text-muted-foreground';

export const TD_CLASS = 'border-b border-border/60 px-3 py-2.5 align-middle text-sm text-foreground';

/** 行的悬浮反馈；最后一行去掉下边框（卡片自己就是边界）。 */
export const TR_CLASS = 'transition-colors hover:bg-accent/40 last:[&>td]:border-b-0';

/**
 * 空态 / 加载态的一整行占位。
 *
 * 空态给图标 + 说明 + 可选操作：只说「暂无数据」会让人不知道下一步做什么，
 * 尤其是筛选到零结果时。
 */
export function TableStatusRow({
  colSpan,
  loading,
  text,
  icon = true,
  action,
}: {
  colSpan: number;
  loading?: boolean;
  text: string;
  icon?: boolean;
  action?: ReactNode;
}) {
  return (
    <tr>
      <td colSpan={colSpan} className="px-3 py-14 text-center">
        <div className="flex flex-col items-center gap-2 text-xs text-muted-foreground">
          {loading ? (
            <Loader2 className="size-5 animate-spin" aria-hidden="true" />
          ) : icon ? (
            <Inbox className="size-6 opacity-40" aria-hidden="true" />
          ) : null}
          <span role={loading ? 'status' : undefined}>{text}</span>
          {action}
        </div>
      </td>
    </tr>
  );
}

/** 卡片页脚：左区间计数，右分页控件。 */
export function TableFooter({ children }: { children: ReactNode }) {
  return (
    <div className="flex flex-wrap items-center justify-between gap-2 border-t border-border/70 bg-muted/20 px-3 py-2">
      {children}
    </div>
  );
}

/** 页码窗口：总页数少时全列，多时保留首尾与当前页附近。 */
export function pageWindow(page: number, pageCount: number, span = 2): number[] {
  if (pageCount <= 0) return [];
  if (pageCount <= 5 + span * 2) return Array.from({ length: pageCount }, (_, i) => i + 1);

  const start = Math.max(2, page - span);
  const end = Math.min(pageCount - 1, page + span);
  const pages = [1];
  for (let i = start; i <= end; i += 1) pages.push(i);
  pages.push(pageCount);
  return pages;
}

export interface PaginationProps {
  /** 当前页，从 1 开始。 */
  page: number;
  pageCount: number;
  total: number;
  pageSize: number;
  onPageChange: (page: number) => void;
  /** 传入即显示「每页 N 条」选择器。 */
  onPageSizeChange?: (size: number) => void;
  pageSizeOptions?: number[];
  disabled?: boolean;
}

/**
 * 分页器：区间计数 + 页码 + 上一页/下一页 + 每页条数。
 *
 * 页码全列（后台数据量级小，省略号反而让人数不清第几页），超过 7 页时用
 * `pageWindow` 取窗口，断档处显示不可点的省略号。
 */
export function Pagination({
  page,
  pageCount,
  total,
  pageSize,
  onPageChange,
  onPageSizeChange,
  pageSizeOptions = [10, 20, 50, 100],
  disabled = false,
}: PaginationProps) {
  const pages = pageWindow(page, pageCount);
  const first = total === 0 ? 0 : (page - 1) * pageSize + 1;
  const last = Math.min(page * pageSize, total);

  return (
    <>
      <span className="text-[11px] text-muted-foreground tabular-nums">
        {total === 0 ? '共 0 条' : `第 ${first}–${last} 条 / 共 ${total} 条`}
      </span>

      <div className="flex flex-wrap items-center gap-2">
        {onPageSizeChange && (
          <label className="flex items-center gap-1 text-[11px] text-muted-foreground">
            每页
            <Select
              items={pageSizeOptions.map((size) => ({ value: size, label: String(size) }))}
              value={pageSize}
              onValueChange={(value) => onPageSizeChange(Number(value))}
              disabled={disabled}
            >
              <SelectTrigger aria-label="每页条数" className="h-6 w-[4.5rem] px-1.5 text-[11px]">
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {pageSizeOptions.map((size) => (
                  <SelectItem key={size} value={size}>
                    {size}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
            条
          </label>
        )}

        <nav aria-label="分页" className="flex items-center gap-0.5">
          <Button
            variant="ghost"
            size="icon-xs"
            onClick={() => onPageChange(page - 1)}
            disabled={disabled || page <= 1}
            aria-label="上一页"
            className="cursor-pointer"
          >
            <ChevronLeft aria-hidden="true" />
          </Button>

          {pages.map((value, index) => {
            const previous = pages[index - 1];
            const gap = previous !== undefined && value - previous > 1;
            return (
              <span key={value} className="flex items-center gap-0.5">
                {gap && (
                  <span className="px-1 text-[11px] text-muted-foreground" aria-hidden="true">
                    …
                  </span>
                )}
                <Button
                  variant={value === page ? 'default' : 'ghost'}
                  size="icon-xs"
                  onClick={() => onPageChange(value)}
                  disabled={disabled}
                  aria-label={`第 ${value} 页`}
                  aria-current={value === page ? 'page' : undefined}
                  className="cursor-pointer tabular-nums"
                >
                  {value}
                </Button>
              </span>
            );
          })}

          <Button
            variant="ghost"
            size="icon-xs"
            onClick={() => onPageChange(page + 1)}
            disabled={disabled || page >= pageCount}
            aria-label="下一页"
            className="cursor-pointer"
          >
            <ChevronRight aria-hidden="true" />
          </Button>
        </nav>
      </div>
    </>
  );
}

/** 加载中 / 失败时的整页提示条（持续状态用它，不用 toast）。 */
export function InlineAlert({
  tone = 'error',
  children,
  action,
}: {
  tone?: 'error' | 'warning' | 'info';
  children: ReactNode;
  action?: ReactNode;
}) {
  const toneClass = {
    error: 'border-destructive/40 bg-destructive/10 text-destructive',
    warning: 'border-amber-500/40 bg-amber-500/10 text-amber-700 dark:text-amber-300',
    info: 'border-border bg-muted/50 text-muted-foreground',
  }[tone];

  return (
    <div
      role={tone === 'error' ? 'alert' : 'status'}
      className={cn('flex flex-wrap items-center gap-2 rounded-lg border px-3 py-2 text-xs', toneClass)}
    >
      <span className="min-w-0 flex-1 break-words">{children}</span>
      {action}
    </div>
  );
}
