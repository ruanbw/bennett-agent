import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { ChevronLeft, ChevronRight, Loader2, Search, X } from 'lucide-react';

import { Button } from '../ui/button';
import { ConfirmDialog } from '../ui/confirm-dialog';
import { Input } from '../ui/input';
import {
  banUser,
  describeFailure,
  listUsers,
  unbanUser,
  type AdminUser,
  type AdminUserPage,
} from '../../lib/admin';

/** 每页条数：与后端的 limit 上限无关，纯展示选择。 */
const PAGE_SIZE = 20;

export interface UsersPanelProps {
  /** 当前登录邮箱：自己不能被自己封禁。 */
  currentUserEmail: string | null;
}

function isSelf(user: AdminUser, currentUserEmail: string | null): boolean {
  if (!currentUserEmail) return false;
  // 后端不会给出同一邮箱的不同大小写形式，但比较时统一小写不会有副作用
  return user.email.toLowerCase() === currentUserEmail.toLowerCase();
}

export function UsersPanel({ currentUserEmail }: UsersPanelProps) {
  const [query, setQuery] = useState('');
  const [appliedQuery, setAppliedQuery] = useState('');
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<AdminUserPage | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);

  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [pendingBan, setPendingBan] = useState<AdminUser | null>(null);

  const reload = useCallback(() => setReloadToken((token) => token + 1), []);

  useEffect(() => {
    let active = true;

    void (async () => {
      try {
        const result = await listUsers({ query: appliedQuery, limit: PAGE_SIZE, offset });
        if (!active) return;
        setPage(result);
        setLoadError(null);
      } catch (err) {
        if (!active) return;
        console.error('加载用户列表失败:', err);
        setLoadError(describeFailure(err));
      }
    })();

    return () => {
      active = false;
    };
  }, [appliedQuery, offset, reloadToken]);

  const run = useCallback(
    async (key: string, action: () => Promise<void>): Promise<boolean> => {
      setBusy(key);
      setActionError(null);
      try {
        await action();
        return true;
      } catch (err) {
        console.error(`用户管理操作失败 (${key}):`, err);
        setActionError(describeFailure(err));
        return false;
      } finally {
        setBusy(null);
      }
    },
    [],
  );

  const handleSearch = useCallback(() => {
    setOffset(0);
    setAppliedQuery(query.trim());
  }, [query]);

  const handleClearSearch = useCallback(() => {
    setQuery('');
    setOffset(0);
    setAppliedQuery('');
  }, []);

  const handleBan = useCallback(
    (user: AdminUser) => {
      void (async () => {
        const ok = await run(`ban:${user.id}`, async () => {
          await banUser(user.id);
        });
        if (ok) reload();
      })();
    },
    [run, reload],
  );

  const handleUnban = useCallback(
    (user: AdminUser) => {
      void (async () => {
        const ok = await run(`unban:${user.id}`, async () => {
          await unbanUser(user.id);
        });
        if (ok) reload();
      })();
    },
    [run, reload],
  );

  const total = page?.total ?? 0;
  const pageIndex = Math.floor(offset / PAGE_SIZE);
  const pageCount = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const isBusy = busy !== null;

  return (
    <section className="space-y-3" aria-labelledby="admin-users-heading">
      <div>
        <h2 id="admin-users-heading" className="text-sm font-semibold text-foreground">
          用户管理
        </h2>
        <p className="mt-0.5 text-xs text-muted-foreground">
          按邮箱检索账号，封禁会立即断开该用户的所有会话。
        </p>
      </div>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          handleSearch();
        }}
        className="flex flex-wrap items-center gap-1.5"
      >
        <div className="relative min-w-[12rem] flex-1">
          <Search
            className="pointer-events-none absolute left-2.5 top-1/2 size-3.5 -translate-y-1/2 text-muted-foreground"
            aria-hidden="true"
          />
          <Input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="按邮箱搜索"
            aria-label="按邮箱搜索用户"
            autoComplete="off"
            className="h-8 pl-8 text-xs"
          />
        </div>
        <Button type="submit" size="sm" disabled={isBusy} className="cursor-pointer">
          搜索
        </Button>
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={handleClearSearch}
          disabled={isBusy || (!query && !appliedQuery)}
          className="cursor-pointer gap-1"
        >
          <X className="size-3.5" aria-hidden="true" />
          清空
        </Button>
      </form>

      {actionError && (
        <div
          role="alert"
          className="flex items-start gap-2 rounded-xl border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive"
        >
          <span className="flex-1 break-words">{actionError}</span>
          <button
            type="button"
            onClick={() => setActionError(null)}
            aria-label="关闭错误提示"
            className="shrink-0 cursor-pointer hover:opacity-70"
          >
            <X className="size-3.5" aria-hidden="true" />
          </button>
        </div>
      )}

      {loadError && (
        <div
          role="alert"
          className="flex flex-wrap items-center gap-2 rounded-xl border border-destructive/40 bg-destructive/10 px-3 py-2 text-xs text-destructive"
        >
          <span className="min-w-0 flex-1 break-words">{loadError}</span>
          <Button variant="outline" size="xs" onClick={reload} className="cursor-pointer">
            重试
          </Button>
        </div>
      )}

      {page === null ? (
        !loadError && (
          <div
            role="status"
            className="flex items-center justify-center gap-2 rounded-xl border border-border/70 bg-card/50 py-10 text-xs text-muted-foreground"
          >
            <Loader2 className="size-4 animate-spin" aria-hidden="true" />
            正在加载用户…
          </div>
        )
      ) : page.items.length === 0 ? (
        <div className="rounded-xl border border-border/70 bg-card/50 px-4 py-10 text-center text-xs text-muted-foreground">
          {appliedQuery ? `没有匹配「${appliedQuery}」的用户` : '暂无用户'}
        </div>
      ) : (
        <ul className="divide-y divide-border/70 overflow-hidden rounded-xl border border-border/70 bg-card/50">
          {page.items.map((user) => {
            const self = isSelf(user, currentUserEmail);
            const banBlocked = user.is_superuser || self;
            const blockedReason = user.is_superuser
              ? '超级管理员不能被封禁'
              : self
                ? '不能封禁当前登录的账号'
                : undefined;

            return (
              <li
                key={user.id}
                className="flex flex-wrap items-center justify-between gap-2 px-3 py-2.5"
              >
                <div className="min-w-0 flex-1 space-y-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="break-all text-xs text-foreground">{user.email}</span>
                    <Badge tone={user.is_active ? 'ok' : 'danger'}>
                      {user.is_active ? '正常' : '已封禁'}
                    </Badge>
                    {user.is_superuser && <Badge tone="primary">超级管理员</Badge>}
                  </div>
                  {!user.is_verified && (
                    <p className="text-[11px] text-muted-foreground">邮箱未验证</p>
                  )}
                </div>

                <div className="flex items-center gap-1.5">
                  {user.is_active ? (
                    <Button
                      variant="destructive"
                      size="xs"
                      disabled={isBusy || banBlocked}
                      title={blockedReason}
                      onClick={() => setPendingBan(user)}
                      aria-label={`封禁 ${user.email}`}
                      className="cursor-pointer"
                    >
                      封禁
                    </Button>
                  ) : (
                    <Button
                      variant="outline"
                      size="xs"
                      disabled={isBusy}
                      onClick={() => handleUnban(user)}
                      aria-label={`解封 ${user.email}`}
                      className="cursor-pointer"
                    >
                      解封
                    </Button>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}

      <div className="flex flex-wrap items-center justify-between gap-2 text-[11px] text-muted-foreground">
        <span>共 {total} 个用户</span>
        <div className="flex flex-wrap items-center gap-1.5">
          <Button
            variant="outline"
            size="xs"
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            disabled={isBusy || offset === 0}
            aria-label="上一页"
            className="cursor-pointer gap-0.5"
          >
            <ChevronLeft className="size-3.5" aria-hidden="true" />
            上一页
          </Button>
          <span>
            第 {pageIndex + 1} / {pageCount} 页
          </span>
          <Button
            variant="outline"
            size="xs"
            onClick={() => setOffset(offset + PAGE_SIZE)}
            disabled={isBusy || offset + PAGE_SIZE >= total}
            aria-label="下一页"
            className="cursor-pointer gap-0.5"
          >
            下一页
            <ChevronRight className="size-3.5" aria-hidden="true" />
          </Button>
        </div>
      </div>

      <ConfirmDialog
        open={pendingBan !== null}
        onOpenChange={(open) => {
          if (!open) setPendingBan(null);
        }}
        title="封禁用户"
        description={
          pendingBan
            ? `封禁后 ${pendingBan.email} 将立即被踢下线，且无法重新登录；解封后可恢复登录。`
            : undefined
        }
        confirmLabel="封禁"
        destructive
        onConfirm={() => {
          if (pendingBan) void handleBan(pendingBan);
        }}
      />
    </section>
  );
}

interface BadgeProps {
  tone: 'ok' | 'danger' | 'primary';
  children: ReactNode;
}

const badgeToneClass: Record<BadgeProps['tone'], string> = {
  ok: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
  danger: 'bg-destructive/10 text-destructive',
  primary: 'bg-primary/10 text-primary',
};

function Badge({ tone, children }: BadgeProps) {
  return (
    <span
      className={`rounded-full px-1.5 py-0.5 text-[10px] font-medium ${badgeToneClass[tone]}`}
    >
      {children}
    </span>
  );
}
