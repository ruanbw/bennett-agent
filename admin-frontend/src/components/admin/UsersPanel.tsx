/**
 * 用户管理列表。
 *
 * ## 这一页的形状就是经典后台列表页
 *
 * 筛选（邮箱搜索 + 状态分段）→ 卡片表格（多选 + 行操作）→ 页脚分页。
 * 三个通用能力交给成熟的库，页面自己只写「列怎么画」与「动作做什么」：
 *
 * - **表格状态**（多选、全选/半选、行模型）→ `@tanstack/react-table`；
 * - **服务端数据**（缓存、失效、重取、错误重试）→ `@tanstack/react-query`；
 * - **批量动作的反馈** → base-ui Toast（一次动作的结果才用 toast，
 *   「列表加载失败」这类持续状态留在页面内联条上）。
 *
 * ## 分页是服务端分页
 *
 * 用户量可以是几万，不能一次拉全量再在前端切页。所以 `pageIndex` / `pageSize`
 * 都是查询参数，`pageCount` 由后端返回的 `total` 推导；翻页 = 换 query key。
 * 封掉最后一页的最后一个人时 offset 会越界，用 `clampUsersOffset` 夹回来。
 *
 * ## 批量封禁的顺序语义
 *
 * 逐个发请求而不是新加一个批量接口：后端每个封禁都要做等级校验与会话清理，
 * 单个失败不该让整批回滚（「A 已封、B 权限不够」比「整批失败」更接近管理员的本意）。
 * 所以按顺序串行执行并统计结果，最后一条 toast 说明「成功几个、失败几个、为什么」。
 */

import { useCallback, useMemo, useState } from 'react';
import {
  type ColumnDef,
  type RowSelectionState,
  flexRender,
  getCoreRowModel,
  useReactTable,
} from '@tanstack/react-table';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Loader2, Search, ShieldCheck, ShieldMinus, ShieldX, UserX, X } from 'lucide-react';

import { Button } from '@/components/ui/button';
import { Checkbox } from '@/components/ui/checkbox';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { Input } from '@/components/ui/input';
import { notify } from '@/lib/notify';
import { queryKeys } from '@/lib/query-keys';
import {
  ROLE_LABELS,
  banUser,
  clampUsersOffset,
  describeFailure,
  listUsers,
  setUserRole,
  unbanUser,
  type AdminUser,
  type SettableRole,
  type UserStatusFilter,
} from '@/lib/admin';
import {
  Badge,
  InlineAlert,
  PageShell,
  Pagination,
  TableFooter,
  TableStatusRow,
  TableWrap,
  TD_CLASS,
  TH_CLASS,
  TR_CLASS,
  Toolbar,
} from './table-parts';

const DEFAULT_PAGE_SIZE = 20;

const STATUS_OPTIONS: Array<{ value: UserStatusFilter; label: string }> = [
  { value: 'all', label: '全部' },
  { value: 'active', label: '正常' },
  { value: 'banned', label: '已封禁' },
];

export interface UsersPanelProps {
  /** 当前登录邮箱：自己不能被自己封禁，也不能改自己的角色。 */
  currentUserEmail: string | null;
  /** 只有 superadmin 能改角色；后端同样会拦，这里只是不让按钮点了才报错。 */
  canManageRoles: boolean;
}

function isSelf(user: AdminUser, currentUserEmail: string | null): boolean {
  if (!currentUserEmail) return false;
  return user.email.toLowerCase() === currentUserEmail.toLowerCase();
}

/**
 * 一行能不能被「批量动作」碰到，以及不能的原因。
 *
 * 前端的禁用只是提前告知：真正的裁决在后端（等级校验），但让按钮可点再报错
 * 会让管理员以为「点一下就好」，白白多一轮往返。
 */
function bulkBlockReason(user: AdminUser, canManageRoles: boolean): string | null {
  if (user.role === 'superadmin') return '超级管理员只能经运维通道调整';
  if (!canManageRoles && user.role !== 'user') return '只有超级管理员可以管理管理员账号';
  return null;
}

export function UsersPanel({ currentUserEmail, canManageRoles }: UsersPanelProps) {
  const queryClient = useQueryClient();

  /** 已提交的筛选（与输入框分离：输入不触发请求，点搜索/回车才提交）。 */
  const [filters, setFilters] = useState<{ query: string; status: UserStatusFilter }>({
    query: '',
    status: 'all',
  });
  const [draftQuery, setDraftQuery] = useState('');
  const [pageIndex, setPageIndex] = useState(0);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  const [rowSelection, setRowSelection] = useState<RowSelectionState>({});

  const [pendingBan, setPendingBan] = useState<AdminUser | null>(null);
  const [pendingRole, setPendingRole] = useState<{ user: AdminUser; role: SettableRole } | null>(null);
  const [pendingBulk, setPendingBulk] = useState<{ action: 'ban' | 'unban'; users: AdminUser[] } | null>(
    null,
  );

  const params = {
    query: filters.query,
    status: filters.status,
    limit: pageSize,
    offset: pageIndex * pageSize,
  };

  const usersQuery = useQuery({
    queryKey: queryKeys.users(params),
    queryFn: () => listUsers(params),
  });

  // 封掉最后一页的最后一个人后 offset 越界：夹回最后一页而不是把「永远空着的一页」给管理员
  const clampedOffset = usersQuery.data
    ? clampUsersOffset(params.offset, pageSize, usersQuery.data.total)
    : params.offset;
  if (usersQuery.data && clampedOffset !== params.offset) {
    setPageIndex(Math.floor(clampedOffset / pageSize));
  }

  const page = usersQuery.data;
  const rows = useMemo(() => page?.items ?? [], [page]);
  const total = page?.total ?? 0;
  const pageCount = Math.max(1, Math.ceil(total / pageSize));

  const mutate = useMutation({
    mutationFn: async (input: { kind: 'ban'; user: AdminUser } | { kind: 'unban'; user: AdminUser } | { kind: 'role'; user: AdminUser; role: SettableRole }) => {
      if (input.kind === 'ban') return banUser(input.user.id);
      if (input.kind === 'unban') return unbanUser(input.user.id);
      return setUserRole(input.user.id, input.role);
    },
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: ['admin', 'users'] });
    },
  });

  const handleBan = useCallback(
    async (user: AdminUser) => {
      try {
        await mutate.mutateAsync({ kind: 'ban', user });
        notify('success', `已封禁 ${user.email}，该用户的所有会话已失效`);
      } catch (err) {
        console.error('封禁用户失败:', err);
        notify('error', describeFailure(err));
      }
    },
    [mutate],
  );

  const handleUnban = useCallback(
    async (user: AdminUser) => {
      try {
        await mutate.mutateAsync({ kind: 'unban', user });
        notify('success', `已解封 ${user.email}`);
      } catch (err) {
        console.error('解封用户失败:', err);
        notify('error', describeFailure(err));
      }
    },
    [mutate],
  );

  const handleSetRole = useCallback(
    async (user: AdminUser, role: SettableRole) => {
      setPendingRole(null);
      try {
        await mutate.mutateAsync({ kind: 'role', user, role });
        notify('success', `${user.email} 已设为${ROLE_LABELS[role]}，需重新登录生效`);
      } catch (err) {
        console.error('修改角色失败:', err);
        notify('error', describeFailure(err));
      }
    },
    [mutate],
  );

  /**
   * 批量封禁 / 解封：串行逐个提交，失败不中断。
   *
   * 顺序执行而不是 `Promise.all`：这批动作会各自删会话、回收沙箱，并发打过去
   * 既没有收益（人是逐个看的）又让失败归属变模糊。
   */
  const handleBulk = useCallback(
    async (action: 'ban' | 'unban', users: AdminUser[]) => {
      setPendingBulk(null);
      let ok = 0;
      const failures: string[] = [];

      for (const user of users) {
        try {
          await mutate.mutateAsync({ kind: action, user });
          ok += 1;
        } catch (err) {
          failures.push(`${user.email}：${describeFailure(err)}`);
        }
      }

      const verb = action === 'ban' ? '封禁' : '解封';
      if (failures.length === 0) {
        notify('success', `已${verb} ${ok} 个账号`);
      } else if (ok === 0) {
        notify('error', `${verb}失败：${failures[0]}${failures.length > 1 ? `（共 ${failures.length} 个失败）` : ''}`);
      } else {
        notify('error', `已${verb} ${ok} 个，${failures.length} 个失败：${failures[0]}`);
      }
      setRowSelection({});
    },
    [mutate],
  );

  const submitSearch = useCallback(() => {
    setPageIndex(0);
    setRowSelection({});
    setFilters({ query: draftQuery.trim(), status: filters.status });
  }, [draftQuery, filters.status]);

  const changeStatus = useCallback((status: UserStatusFilter) => {
    setPageIndex(0);
    setRowSelection({});
    setFilters((prev) => ({ ...prev, status }));
  }, []);

  const resetFilters = useCallback(() => {
    setDraftQuery('');
    setPageIndex(0);
    setRowSelection({});
    setFilters({ query: '', status: 'all' });
  }, []);

  const selectedUsers = useMemo(
    () => rows.filter((user) => rowSelection[user.id]),
    [rows, rowSelection],
  );

  const columns = useMemo<ColumnDef<AdminUser>[]>(
    () => [
      {
        id: 'select',
        header: ({ table }) => (
          <Checkbox
            aria-label="全选本页用户"
            checked={table.getIsAllPageRowsSelected()}
            indeterminate={table.getIsSomePageRowsSelected()}
            onCheckedChange={(checked) => table.toggleAllPageRowsSelected(checked === true)}
          />
        ),
        cell: ({ row }) => {
          const blocked = bulkBlockReason(row.original, canManageRoles) !== null;
          return (
            <Checkbox
              aria-label={`选择 ${row.original.email}`}
              checked={row.getIsSelected()}
              disabled={blocked || row.original.email === currentUserEmail}
              onCheckedChange={(checked) => row.toggleSelected(checked === true)}
            />
          );
        },
        size: 36,
        enableSorting: false,
      },
      {
        id: 'email',
        header: '邮箱',
        accessorFn: (user) => user.email,
        cell: ({ row }) => {
          const user = row.original;
          const self = isSelf(user, currentUserEmail);
          return (
            <div className="min-w-0">
              <div className="flex items-center gap-1.5">
                <span className="truncate text-xs">{user.email}</span>
                {self && <Badge tone="muted">本人</Badge>}
              </div>
              {!user.is_verified && (
                <div className="mt-0.5 text-[11px] text-muted-foreground">邮箱未验证</div>
              )}
            </div>
          );
        },
      },
      {
        id: 'status',
        header: '状态',
        accessorFn: (user) => (user.is_active ? 'active' : 'banned'),
        cell: ({ row }) => (
          <Badge tone={row.original.is_active ? 'ok' : 'danger'}>
            {row.original.is_active ? '正常' : '已封禁'}
          </Badge>
        ),
      },
      {
        id: 'role',
        header: '角色',
        accessorFn: (user) => user.role,
        cell: ({ row }) => {
          const { role } = row.original;
          if (role === 'user') return <span className="text-xs text-muted-foreground">普通用户</span>;
          return (
            <Badge tone={role === 'superadmin' ? 'primary' : 'muted'}>{ROLE_LABELS[role] ?? role}</Badge>
          );
        },
      },
      {
        id: 'actions',
        header: () => <span className="block text-right">操作</span>,
        cell: ({ row }) => {
          const user = row.original;
          const self = isSelf(user, currentUserEmail);
          const banBlocked = self || bulkBlockReason(user, canManageRoles) !== null;
          const banReason = self
            ? '不能封禁当前登录的账号'
            : bulkBlockReason(user, canManageRoles) ?? undefined;
          const roleEditable =
            canManageRoles && !self && user.is_active && user.role !== 'superadmin';
          const nextRole: SettableRole = user.role === 'admin' ? 'user' : 'admin';
          const busy = mutate.isPending;

          return (
            <div className="flex items-center justify-end gap-1">
              {canManageRoles && user.role !== 'superadmin' && (
                <Button
                  variant="ghost"
                  size="xs"
                  disabled={busy || !roleEditable}
                  title={
                    !canManageRoles
                      ? '只有超级管理员可以调整角色'
                      : self
                        ? '不能修改自己的角色'
                        : !user.is_active
                          ? '该账号已被封禁，请先解封再修改角色'
                          : undefined
                  }
                  onClick={() => setPendingRole({ user, role: nextRole })}
                  aria-label={`把 ${user.email} 设为 ${ROLE_LABELS[nextRole]}`}
                  className="cursor-pointer gap-1"
                >
                  {nextRole === 'admin' ? (
                    <ShieldCheck className="size-3.5" aria-hidden="true" />
                  ) : (
                    <ShieldMinus className="size-3.5" aria-hidden="true" />
                  )}
                  {nextRole === 'admin' ? '设为管理员' : '取消管理员'}
                </Button>
              )}

              {user.is_active ? (
                <Button
                  variant="ghost"
                  size="xs"
                  disabled={busy || banBlocked}
                  title={banReason}
                  onClick={() => setPendingBan(user)}
                  aria-label={`封禁 ${user.email}`}
                  className="cursor-pointer gap-1 text-destructive hover:bg-destructive/10 hover:text-destructive"
                >
                  <UserX className="size-3.5" aria-hidden="true" />
                  封禁
                </Button>
              ) : (
                <Button
                  variant="outline"
                  size="xs"
                  disabled={busy}
                  onClick={() => void handleUnban(user)}
                  aria-label={`解封 ${user.email}`}
                  className="cursor-pointer"
                >
                  解封
                </Button>
              )}
            </div>
          );
        },
        size: 200,
      },
    ],
    [canManageRoles, currentUserEmail, handleUnban, mutate.isPending],
  );

  const table = useReactTable({
    data: rows,
    columns,
    state: { rowSelection },
    onRowSelectionChange: setRowSelection,
    getRowId: (user) => user.id,
    enableRowSelection: (row) =>
      !isSelf(row.original, currentUserEmail) && bulkBlockReason(row.original, canManageRoles) === null,
    getCoreRowModel: getCoreRowModel(),
    // 服务端分页：不做前端分页/排序，页码由后端 total 推导
    manualPagination: true,
    pageCount,
  });

  const columnCount = columns.length;
  const hasFilters = filters.query !== '' || filters.status !== 'all';
  const emptyText = filters.query
    ? `没有匹配「${filters.query}」的用户`
    : filters.status === 'banned'
      ? '没有被封禁的账号'
      : filters.status === 'active'
        ? '没有正常状态的账号'
        : '还没有任何用户';

  return (
    <PageShell
      title="用户管理"
      description={
        <>
          按邮箱检索账号，封禁会立即断开该用户的所有会话，解封只恢复登录能力（不恢复旧会话）。
          {canManageRoles ? '修改角色同样立即生效，对方需重新登录。' : '角色设置仅超级管理员可用。'}
        </>
      }
    >
      <Toolbar aria-label="用户筛选">
        <form
          onSubmit={(event) => {
            event.preventDefault();
            submitSearch();
          }}
          className="flex min-w-0 flex-wrap items-center gap-1.5"
        >
          <div className="relative w-full max-w-xs min-w-48">
            <Search
              className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground"
              aria-hidden="true"
            />
            <Input
              value={draftQuery}
              onChange={(event) => setDraftQuery(event.target.value)}
              placeholder="按邮箱搜索"
              aria-label="按邮箱搜索用户"
              autoComplete="off"
              className="h-8 pl-8 md:text-xs"
            />
          </div>
          <Button type="submit" size="sm" disabled={usersQuery.isFetching} className="cursor-pointer">
            搜索
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            onClick={resetFilters}
            disabled={!hasFilters && draftQuery === ''}
            className="cursor-pointer gap-1"
          >
            <X className="size-3.5" aria-hidden="true" />
            重置
          </Button>
        </form>

        {/* 状态分段：账号一多，「有哪些被封了」不能靠翻页找人 */}
        <div role="group" aria-label="按账号状态筛选" className="ml-auto flex items-center gap-1">
          {STATUS_OPTIONS.map(({ value, label }) => (
            <Button
              key={value}
              type="button"
              size="xs"
              variant={filters.status === value ? 'default' : 'outline'}
              aria-pressed={filters.status === value}
              onClick={() => changeStatus(value)}
              className="cursor-pointer"
            >
              {label}
            </Button>
          ))}
        </div>
      </Toolbar>

      {usersQuery.isError && (
        <InlineAlert
          action={
            <Button variant="outline" size="xs" onClick={() => void usersQuery.refetch()} className="cursor-pointer">
              重试
            </Button>
          }
        >
          {describeFailure(usersQuery.error)}
        </InlineAlert>
      )}

      <TableWrap
        minWidth="52rem"
        toolbar={
          // 批量操作条：只在有选中项时出现，避免平时占一行空间还不知道能干什么
          selectedUsers.length > 0 ? (
            <div className="flex flex-wrap items-center gap-2 border-b border-border/70 bg-primary/5 px-3 py-2 text-xs">
              <span className="font-medium text-foreground">已选 {selectedUsers.length} 个</span>
              <span className="text-muted-foreground">（仅本页）</span>
              <div className="ml-auto flex items-center gap-1.5">
                <Button
                  variant="outline"
                  size="xs"
                  onClick={() => setPendingBulk({ action: 'unban', users: selectedUsers.filter((u) => !u.is_active) })}
                  disabled={mutate.isPending || selectedUsers.every((u) => u.is_active)}
                  className="cursor-pointer"
                >
                  批量解封
                </Button>
                <Button
                  variant="destructive"
                  size="xs"
                  onClick={() => setPendingBulk({ action: 'ban', users: selectedUsers.filter((u) => u.is_active) })}
                  disabled={mutate.isPending || selectedUsers.every((u) => !u.is_active)}
                  className="cursor-pointer"
                >
                  批量封禁
                </Button>
                <Button
                  variant="ghost"
                  size="xs"
                  onClick={() => setRowSelection({})}
                  className="cursor-pointer gap-1"
                >
                  <X className="size-3" aria-hidden="true" />
                  取消选择
                </Button>
              </div>
            </div>
          ) : null
        }
        footer={
          <TableFooter>
            <Pagination
              page={pageIndex + 1}
              pageCount={pageCount}
              total={total}
              pageSize={pageSize}
              disabled={usersQuery.isFetching}
              onPageChange={(next) => {
                setPageIndex(next - 1);
                setRowSelection({});
              }}
              onPageSizeChange={(size) => {
                setPageSize(size);
                setPageIndex(0);
                setRowSelection({});
              }}
            />
          </TableFooter>
        }
      >
        <thead>
          {table.getHeaderGroups().map((headerGroup) => (
            <tr key={headerGroup.id}>
              {headerGroup.headers.map((header) => (
                <th
                  key={header.id}
                  className={TH_CLASS}
                  style={{ width: header.getSize() === 150 ? undefined : header.getSize() }}
                >
                  {header.isPlaceholder ? null : flexRender(header.column.columnDef.header, header.getContext())}
                </th>
              ))}
            </tr>
          ))}
        </thead>
        <tbody
          aria-busy={usersQuery.isFetching}
          className={usersQuery.isFetching ? 'opacity-60 transition-opacity' : 'transition-opacity'}
        >
          {usersQuery.isPending ? (
            <TableStatusRow colSpan={columnCount} loading text="正在加载用户…" />
          ) : rows.length === 0 ? (
            <TableStatusRow
              colSpan={columnCount}
              text={emptyText}
              action={
                hasFilters ? (
                  <Button variant="outline" size="xs" onClick={resetFilters} className="cursor-pointer">
                    清除筛选
                  </Button>
                ) : undefined
              }
            />
          ) : (
            table.getRowModel().rows.map((row) => (
              <tr key={row.id} className={TR_CLASS} data-state={row.getIsSelected() ? 'selected' : undefined}>
                {row.getVisibleCells().map((cell) => (
                  <td
                    key={cell.id}
                    className={TD_CLASS}
                    style={{
                      width: cell.column.id === 'select' ? undefined : cell.column.getSize() === 150 ? undefined : cell.column.getSize(),
                    }}
                  >
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </td>
                ))}
              </tr>
            ))
          )}
        </tbody>
      </TableWrap>

      {/* 列头勾选与行勾选都靠工具条上的按钮；这里补一条读屏状态 */}
      <p className="sr-only" role="status">
        {selectedUsers.length > 0 ? `已选择 ${selectedUsers.length} 个用户` : '未选择任何用户'}
      </p>

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
          setPendingBan(null);
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
          if (pendingRole) void handleSetRole(pendingRole.user, pendingRole.role);
        }}
      />

      <ConfirmDialog
        open={pendingBulk !== null}
        onOpenChange={(open) => {
          if (!open) setPendingBulk(null);
        }}
        title={pendingBulk?.action === 'ban' ? '批量封禁' : '批量解封'}
        description={
          pendingBulk
            ? pendingBulk.action === 'ban'
              ? `将封禁 ${pendingBulk.users.length} 个账号，它们的会话会立即失效。失败的会单独提示，不会影响其他账号。`
              : `将解封 ${pendingBulk.users.length} 个账号，恢复登录能力（旧会话不恢复）。`
            : undefined
        }
        confirmLabel={pendingBulk?.action === 'ban' ? '全部封禁' : '全部解封'}
        destructive={pendingBulk?.action === 'ban'}
        confirmDisabled={(pendingBulk?.users.length ?? 0) === 0}
        onConfirm={() => {
          if (pendingBulk) void handleBulk(pendingBulk.action, pendingBulk.users);
        }}
      />

      {/* 长任务的进度提示：批量串行时按钮已禁用，这里再把「正在做什么」说清楚 */}
      {mutate.isPending && (
        <p role="status" className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
          <Loader2 className="size-3 animate-spin" aria-hidden="true" />
          正在提交变更…
        </p>
      )}

      <p className="flex items-center gap-1.5 text-[11px] text-muted-foreground">
        <ShieldX className="size-3" aria-hidden="true" />
        封禁/解封都不允许跨越等级（admin 不能动 admin 与 superadmin）。
      </p>
    </PageShell>
  );
}
