import { useCallback, useEffect, useState, type ReactNode } from 'react';
import { ChevronLeft, ChevronRight, Loader2, Search, ShieldCheck, X } from 'lucide-react';

import { Button } from '../ui/button';
import { ConfirmDialog } from '../ui/confirm-dialog';
import { Input } from '../ui/input';
import {
  ROLE_LABELS,
  banUser,
  clampUsersOffset,
  describeFailure,
  listUsers,
  setUserRole,
  unbanUser,
  type AdminUser,
  type AdminUserPage,
  type SettableRole,
} from '../../lib/admin';

/** 每页条数：与后端的 limit 上限无关，纯展示选择。 */
const PAGE_SIZE = 20;

export interface UsersPanelProps {
  /** 当前登录邮箱：自己不能被自己封禁，也不能改自己的角色。 */
  currentUserEmail: string | null;
  /**
   * 当前登录账号是否是 superadmin。
   *
   * 只有最高一档能改别人角色；后端同样会拦，这里只是不让按钮点了才报错。
   */
  canManageRoles: boolean;
}

function isSelf(user: AdminUser, currentUserEmail: string | null): boolean {
  if (!currentUserEmail) return false;
  // 统一小写比较：后端不会给同一邮箱的不同大小写形式，但这样不会有副作用
  return user.email.toLowerCase() === currentUserEmail.toLowerCase();
}

/** 角色 → 徒标颜色。`superadmin` 用主色，`admin` 用中性，普通用户不显示徒标。 */
const ROLE_TONE: Record<string, 'primary' | 'muted'> = {
  superadmin: 'primary',
  admin: 'muted',
};

export function UsersPanel({ currentUserEmail, canManageRoles }: UsersPanelProps) {
  const [query, setQuery] = useState('');
  const [appliedQuery, setAppliedQuery] = useState('');
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState<AdminUserPage | null>(null);
  // 请求身份：列表「是否在刷新」由它和最近一次完成的请求推导，而不是在 effect 里
  // setState(true)——那会触发一次级联渲染，React 的 lint 规则也会报。
  const [completedKey, setCompletedKey] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [reloadToken, setReloadToken] = useState(0);

  const [busy, setBusy] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [pendingBan, setPendingBan] = useState<AdminUser | null>(null);
  const [pendingRole, setPendingRole] = useState<{ user: AdminUser; role: SettableRole } | null>(null);

  const reload = useCallback(() => setReloadToken((token) => token + 1), []);

  const requestKey = `${appliedQuery}\u0000${offset}\u0000${reloadToken}`;
  const loading = completedKey !== requestKey;

  useEffect(() => {
    let active = true;

    void (async () => {
      try {
        const result = await listUsers({ query: appliedQuery, limit: PAGE_SIZE, offset });
        if (!active) return;

        // 封掉最后一页的唯一用户后，offset 会越过新的最后一页：夹回去重新拉取，
        // 不要把「永远空着的一页」展示给管理员。
        const nextOffset = clampUsersOffset(offset, PAGE_SIZE, result.total);
        if (nextOffset !== offset) {
          setOffset(nextOffset);
          return;
        }

        setPage(result);
        setLoadError(null);
        setCompletedKey(requestKey);
      } catch (err) {
        if (!active) return;
        console.error('加载用户列表失败:', err);
        setLoadError(describeFailure(err));
        setCompletedKey(requestKey);
      }
    })();

    return () => {
      active = false;
    };
  }, [appliedQuery, offset, reloadToken, requestKey]);

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

  /** 提交角色变更。角色变更是破坏性的：会踢掉对方现有会话。 */
  const handleSetRole = useCallback(
    (user: AdminUser, role: SettableRole) => {
      setPendingRole(null);
      void (async () => {
        const ok = await run(`role:${user.id}`, async () => {
          await setUserRole(user.id, role);
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
  // 列表刷新中也不允许操作：行数据可能马上被替换，别让管理员对着旧行点封禁
  const controlsDisabled = isBusy || loading;

  return (
    <section className="space-y-3" aria-labelledby="admin-users-heading">
      <div>
        <h2 id="admin-users-heading" className="text-sm font-semibold text-foreground">
          用户管理
        </h2>
        <p className="mt-0.5 text-xs text-muted-foreground">
          按邮箱检索账号。封禁会立即断开该用户的所有会话；
          {canManageRoles
            ? '修改角色同样会立即生效（对方需重新登录）。'
            : '角色设置仅超级管理员可用。'}
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
            className="h-8 pl-8 md:text-xs"
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
        <div className="flex flex-col items-center gap-2 rounded-xl border border-border/70 bg-card/50 px-4 py-10 text-center text-xs text-muted-foreground">
          <span>{appliedQuery ? `没有匹配「${appliedQuery}」的用户` : '暂无用户'}</span>
          {appliedQuery && (
            <Button
              variant="outline"
              size="xs"
              onClick={handleClearSearch}
              className="cursor-pointer"
            >
              清除搜索
            </Button>
          )}
        </div>
      ) : (
        <ul
          aria-busy={loading}
          className={`divide-y divide-border/70 overflow-hidden rounded-xl border border-border/70 bg-card/50 transition-opacity ${
            loading ? 'opacity-60' : ''
          }`}
        >
          {page.items.map((user) => {
            const self = isSelf(user, currentUserEmail);
            // 封禁：不能封自己，也不能封同级或更高等级（这里只是提前禁用，后端也会拦）
            const banBlocked = self || (canManageRoles ? false : user.role !== 'user');
            const blockedReason = self
              ? '不能封禁当前登录的账号'
              : canManageRoles
                ? undefined
                : // admin 只能动普通用户：同级之间互封会让后台失去可预期性
                  user.role === 'superadmin' || user.role === 'admin'
                  ? '只有超级管理员可以管理管理员账号'
                  : undefined;

            // 角色按钮：只有 superadmin 能看到；自己、超管、已封禁账号不可改
            const roleEditable = canManageRoles && !self && user.is_active && user.role !== 'superadmin';
            const roleBlockedReason = !canManageRoles
              ? '只有超级管理员可以调整角色'
              : self
                ? '不能修改自己的角色'
                : user.role === 'superadmin'
                  ? '超级管理员只能经运维通道（seed_admin.py）调整'
                  : !user.is_active
                    ? '该账号已被封禁，请先解封再修改角色'
                    : undefined;
            const nextRole: SettableRole = user.role === 'admin' ? 'user' : 'admin';

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
                    {/* 普通用户不戴角色徒标：徒标太多时“正常”两个字才是重点 */}
                    {user.role !== 'user' && (
                      <Badge tone={ROLE_TONE[user.role] ?? 'muted'}>
                        {ROLE_LABELS[user.role] ?? user.role}
                      </Badge>
                    )}
                  </div>
                  {!user.is_verified && (
                    <p className="text-[11px] text-muted-foreground">邮箱未验证</p>
                  )}
                </div>

                <div className="flex items-center gap-1.5">
                  {canManageRoles && user.role !== 'superadmin' && (
                    <Button
                      variant="outline"
                      size="xs"
                      disabled={controlsDisabled || !roleEditable}
                      title={roleBlockedReason}
                      onClick={() => setPendingRole({ user, role: nextRole })}
                      aria-label={`把 ${user.email} 设为 ${ROLE_LABELS[nextRole]}`}
                      className="cursor-pointer gap-1"
                    >
                      <ShieldCheck className="size-3.5" aria-hidden="true" />
                      {nextRole === 'admin' ? '设为管理员' : '取消管理员'}
                    </Button>
                  )}

                  {user.is_active ? (
                    <Button
                      variant="destructive"
                      size="xs"
                      disabled={controlsDisabled || banBlocked}
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
                      disabled={controlsDisabled}
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
        <span className="inline-flex items-center gap-1.5">
          {page === null
            ? loading
              ? '正在加载用户…'
              : '共 0 个用户'
            : total === 0
              ? '共 0 个用户'
              : `第 ${offset + 1}–${Math.min(offset + page.items.length, total)} 个，共 ${total} 个`}
          {loading && page !== null && (
            <span role="status" className="inline-flex items-center gap-1">
              <Loader2 className="size-3 animate-spin" aria-hidden="true" />
              正在刷新…
            </span>
          )}
        </span>
        <div className="flex flex-wrap items-center gap-1.5">
          <Button
            variant="outline"
            size="xs"
            onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
            disabled={controlsDisabled || offset === 0}
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
            disabled={controlsDisabled || offset + PAGE_SIZE >= total}
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

      <ConfirmDialog
        open={pendingRole !== null}
        onOpenChange={(open) => {
          if (!open) setPendingRole(null);
        }}
        title={pendingRole?.role === 'admin' ? '设为管理员' : '取消管理员'}
        description={
          pendingRole
            ? pendingRole.role === 'admin'
              ? `${pendingRole.user.email} 将能进入管理后台、维护模型提供商与封禁普通用户。该用户当前的登录会话会立即失效，需要重新登录。`
              : `${pendingRole.user.email} 将失去管理后台的全部权限，当前会话会立即失效，需要重新登录。`
            : undefined
        }
        confirmLabel={pendingRole?.role === 'admin' ? '设为管理员' : '取消管理员'}
        destructive={pendingRole?.role === 'user'}
        onConfirm={() => {
          if (pendingRole) handleSetRole(pendingRole.user, pendingRole.role);
        }}
      />
    </section>
  );
}

interface BadgeProps {
  tone: 'ok' | 'danger' | 'primary' | 'muted';
  children: ReactNode;
}

const badgeToneClass: Record<BadgeProps['tone'], string> = {
  ok: 'bg-emerald-500/10 text-emerald-600 dark:text-emerald-400',
  danger: 'bg-destructive/10 text-destructive',
  primary: 'bg-primary/10 text-primary',
  muted: 'bg-muted text-muted-foreground',
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
