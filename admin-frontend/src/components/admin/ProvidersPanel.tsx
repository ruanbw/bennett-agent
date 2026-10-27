/**
 * 模型提供商管理。
 *
 * ## 为什么是两个层级的表（而不是一张大表 + 筛选）
 *
 * 提供商是「配置单位」，模型是它的从属数据，一次管理动作只针对一个提供商（同步、测试
 * 连接、编辑密钥）。所以主表是提供商，展开某一行才看到它下面的模型子表——与后台里
 * 「订单 → 订单明细」「角色 → 权限」同一套心智模型。
 *
 * ## 库的分工
 *
 * - 主表的**行模型与展开状态**：`@tanstack/react-table`；
 * - 每个提供商的**模型清单**：`@tanstack/react-query` 按 providerId 缓存，
 *   同步 / 编辑模型后只失效那一条，不必重拉全部提供商；
 * - 动作反馈：base-ui Toast 报「做成了什么、几个」。
 *
 * ## 一次只展开一行
 *
 * 管理动作都针对单个提供商；同时展开多行会让「测试连接」的结果条出现在视线之外。
 * 但模型清单的查询仍然按 providerId 缓存，来回展开不会重复请求。
 */

import { Fragment, useCallback, useMemo, useState } from 'react';
import {
  type ColumnDef,
  type ExpandedState,
  flexRender,
  getCoreRowModel,
  getExpandedRowModel,
  useReactTable,
} from '@tanstack/react-table';
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query';
import {
  ChevronDown,
  ChevronRight,
  Info,
  KeyRound,
  Loader2,
  Pencil,
  PlugZap,
  Plus,
  RefreshCw,
  Search,
  Star,
  Trash2,
  X,
} from 'lucide-react';

import { Button } from '@/components/ui/button';
import { ConfirmDialog } from '@/components/ui/confirm-dialog';
import { Input } from '@/components/ui/input';
import { notify } from '@/lib/notify';
import { queryKeys } from '@/lib/query-keys';
import {
  createProviderModel,
  deleteModel,
  deleteProvider,
  describeFailure,
  describeProviderSync,
  filterAdminModels,
  formatContextWindow,
  listProviderModels,
  listProviders,
  syncProvider,
  testProvider,
  updateModel,
  type AdminModel,
  type AdminProvider,
} from '@/lib/admin';
import { formatDateTime } from '@/lib/format';
import { ModelDetailDialog } from './ModelDetailDialog';
import { ModelDialog } from './ModelDialog';
import { ProviderDialog } from './ProviderDialog';
import { ToggleSwitch } from './ToggleSwitch';
import {
  Badge,
  InlineAlert,
  PageShell,
  TableStatusRow,
  TableWrap,
  TD_CLASS,
  TH_CLASS,
  TR_CLASS,
  Toolbar,
} from './table-parts';

/** 模型清单默认平铺几行，超出折叠（一个 LiteLLM 网关常暴露上百个模型）。 */
const VISIBLE_MODELS_COLLAPSED = 8;

export function ProvidersPanel() {
  const queryClient = useQueryClient();

  const [query, setQuery] = useState('');
  const [expanded, setExpanded] = useState<ExpandedState>({});
  /** 同一时间只挂一个动作 key（`test:<id>`、`model-default:<id>`），用于按钮 spinner。 */
  const [busy, setBusy] = useState<string | null>(null);

  const [dialogOpen, setDialogOpen] = useState(false);
  const [editingProvider, setEditingProvider] = useState<AdminProvider | null>(null);
  const [editingModel, setEditingModel] = useState<AdminModel | null>(null);
  const [detailModel, setDetailModel] = useState<AdminModel | null>(null);
  const [pendingDeleteProvider, setPendingDeleteProvider] = useState<AdminProvider | null>(null);
  const [pendingDeleteModel, setPendingDeleteModel] = useState<AdminModel | null>(null);

  const providersQuery = useQuery({ queryKey: queryKeys.providers, queryFn: listProviders });
  const providers = useMemo(() => providersQuery.data ?? [], [providersQuery.data]);

  // 模型清单：每个提供商一条 query，key 里带 providerId，失效时能只打掉一个
  const modelQueries = useQueries({
    queries: providers.map((provider) => ({
      queryKey: queryKeys.providerModels(provider.id),
      queryFn: () => listProviderModels(provider.id),
    })),
  });

  const modelsByProvider = useMemo(() => {
    const map = new Map<string, AdminModel[]>();
    providers.forEach((provider, index) => {
      const data = modelQueries[index]?.data;
      if (data) map.set(provider.id, data);
    });
    return map;
  }, [providers, modelQueries]);

  /** 让某个提供商的模型清单失效后重取（同步 / 加删模型之后）。 */
  const refreshModels = useCallback(
    (providerId: string) =>
      queryClient.invalidateQueries({ queryKey: queryKeys.providerModels(providerId) }),
    [queryClient],
  );

  const providerMutation = useMutation({
    mutationFn: ({ id }: { id: string }) => deleteProvider(id),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: queryKeys.providers });
    },
  });

  /**
   * 一个统一的动作执行器：挂 busy、把失败翻译成人话、返回是否成功。
   *
   * 管理动作的失败原因基本上是「上游连不上 / 权限不够 / 已被别人删掉」三类，
   * 全部走同一套翻译；调用方只决定成功后要不要刷新。
   */
  const run = useCallback(
    async (key: string, action: () => Promise<void>): Promise<boolean> => {
      setBusy(key);
      try {
        await action();
        return true;
      } catch (err) {
        console.error(`管理操作失败 (${key}):`, err);
        notify('error', describeFailure(err));
        return false;
      } finally {
        setBusy(null);
      }
    },
    [],
  );

  const handleTest = useCallback(
    (provider: AdminProvider) => {
      void run(`test:${provider.id}`, async () => {
        // 「连不上」是测试的预期结果之一：它自己会返回 ok:false，不是异常
        const result = await testProvider(provider.id);
        if (result.ok) {
          notify('success', `${provider.name} 连接正常，上游返回 ${result.model_count} 个模型`);
        } else {
          notify('error', result.error ?? `${provider.name} 连接失败，上游未返回原因`);
        }
      });
    },
    [run],
  );

  const handleSync = useCallback(
    (provider: AdminProvider) => {
      void (async () => {
        const ok = await run(`sync:${provider.id}`, async () => {
          const result = await syncProvider(provider.id);
          notify('success', describeProviderSync(result));
        });
        if (ok) void refreshModels(provider.id);
      })();
    },
    [run, refreshModels],
  );

  const handleToggleModel = useCallback(
    (model: AdminModel) => {
      void (async () => {
        // 停用默认模型时后端会自动取消默认，局部点改会让星标残留 → 统一重取
        const ok = await run(`model-toggle:${model.id}`, async () => {
          await updateModel(model.id, { enabled: !model.enabled });
        });
        if (ok) void refreshModels(model.provider_id);
      })();
    },
    [run, refreshModels],
  );

  const handleSetDefault = useCallback(
    (model: AdminModel) => {
      void (async () => {
        const ok = await run(`model-default:${model.id}`, async () => {
          await updateModel(model.id, { is_default: true });
          notify('success', `已把 ${model.display_name} 设为默认模型`);
        });
        if (ok) void refreshModels(model.provider_id);
      })();
    },
    [run, refreshModels],
  );

  const handleDeleteModel = useCallback(
    (model: AdminModel) => {
      void (async () => {
        const ok = await run(`model-delete:${model.id}`, async () => {
          await deleteModel(model.id);
          notify('success', `已删除模型 ${model.model_id}`);
        });
        if (ok) void refreshModels(model.provider_id);
      })();
    },
    [run, refreshModels],
  );

  const handleAddModel = useCallback(
    async (providerId: string, modelId: string): Promise<boolean> => {
      const trimmed = modelId.trim();
      if (!trimmed) {
        notify('error', '请先填写上游模型 ID');
        return false;
      }
      const ok = await run(`model-create:${providerId}`, async () => {
        await createProviderModel(providerId, { model_id: trimmed });
        notify('success', `已添加模型 ${trimmed}`);
      });
      if (ok) void refreshModels(providerId);
      return ok;
    },
    [run, refreshModels],
  );

  const filteredProviders = useMemo(() => {
    const needle = query.trim().toLowerCase();
    if (!needle) return providers;
    return providers.filter(
      (provider) =>
        provider.name.toLowerCase().includes(needle) ||
        provider.slug.toLowerCase().includes(needle) ||
        provider.base_url.toLowerCase().includes(needle),
    );
  }, [providers, query]);

  const columns = useMemo<ColumnDef<AdminProvider>[]>(
    () => [
      {
        id: 'expander',
        header: () => <span className="sr-only">展开</span>,
        cell: ({ row }) => (
          <button
            type="button"
            onClick={row.getToggleExpandedHandler()}
            aria-expanded={row.getIsExpanded()}
            aria-label={`${row.getIsExpanded() ? '收起' : '展开'} ${row.original.name} 的模型清单`}
            className="flex size-6 cursor-pointer items-center justify-center rounded-md text-muted-foreground transition-colors hover:bg-accent hover:text-foreground focus-visible:ring-1 focus-visible:ring-ring focus-visible:outline-none"
          >
            {row.getIsExpanded() ? (
              <ChevronDown className="size-4" aria-hidden="true" />
            ) : (
              <ChevronRight className="size-4" aria-hidden="true" />
            )}
          </button>
        ),
        size: 36,
      },
      {
        id: 'provider',
        header: '提供商',
        accessorFn: (provider) => provider.name,
        cell: ({ row }) => {
          const provider = row.original;
          return (
            <div className="min-w-0">
              <div className="flex items-center gap-1.5">
                <span className="text-sm font-medium">{provider.name}</span>
                {provider.preset_key && <Badge tone="muted">预设</Badge>}
              </div>
              <div className="mt-0.5 font-mono text-[11px] text-muted-foreground">{provider.slug}</div>
              <div
                className="mt-0.5 max-w-72 truncate text-[11px] text-muted-foreground"
                title={provider.base_url}
              >
                {provider.base_url}
              </div>
            </div>
          );
        },
        size: 260,
      },
      {
        id: 'status',
        header: '状态',
        accessorFn: (provider) => (provider.enabled ? 'enabled' : 'disabled'),
        cell: ({ row }) => {
          const list = modelsByProvider.get(row.original.id);
          const enabledCount = list?.filter((model) => model.enabled).length ?? 0;
          if (!row.original.enabled) return <Badge tone="muted">已停用</Badge>;
          if (list && enabledCount === 0) return <Badge tone="warn">无可用模型</Badge>;
          return <Badge tone="ok">已启用</Badge>;
        },
        size: 100,
      },
      {
        id: 'models',
        header: '模型',
        cell: ({ row }) => {
          const provider = row.original;
          const list = modelsByProvider.get(provider.id);
          if (!list) return <span className="text-xs text-muted-foreground">统计中…</span>;
          const enabledCount = list.filter((model) => model.enabled).length;
          // 对话侧只列「提供商启用 + 模型启用」的行；后台要能管停用项，所以两个数一起写
          return (
            <span
              className="text-xs tabular-nums"
              title="只有已启用的模型才会出现在对话的模型选择器里"
            >
              {provider.enabled ? enabledCount : <span className="text-muted-foreground">—</span>}
              <span className="text-muted-foreground"> / {list.length}</span>
            </span>
          );
        },
        size: 90,
      },
      {
        id: 'key',
        header: '密钥',
        cell: ({ row }) => (
          <span className="inline-flex items-center gap-1 font-mono text-[11px] text-muted-foreground">
            <KeyRound className="size-3" aria-hidden="true" />
            {row.original.api_key_hint ?? '未设置'}
          </span>
        ),
        size: 120,
      },
      {
        id: 'created',
        header: '创建时间',
        accessorFn: (provider) => provider.created_at,
        cell: ({ row }) => (
          <span className="text-xs whitespace-nowrap text-muted-foreground">
            {formatDateTime(row.original.created_at)}
          </span>
        ),
        size: 120,
      },
      {
        id: 'actions',
        header: () => <span className="block text-right">操作</span>,
        cell: ({ row }) => {
          const provider = row.original;
          const isTesting = busy === `test:${provider.id}`;
          const isSyncing = busy === `sync:${provider.id}`;
          return (
            <div className="flex items-center justify-end gap-1">
              <Button
                variant="outline"
                size="xs"
                onClick={() => handleTest(provider)}
                disabled={busy !== null}
                aria-label={`测试 ${provider.name} 的连接`}
                className="cursor-pointer gap-1"
              >
                {isTesting ? (
                  <Loader2 className="size-3 animate-spin" aria-hidden="true" />
                ) : (
                  <PlugZap className="size-3" aria-hidden="true" />
                )}
                测试
              </Button>
              <Button
                variant="outline"
                size="xs"
                onClick={() => handleSync(provider)}
                disabled={busy !== null}
                aria-label={`同步 ${provider.name} 的模型`}
                className="cursor-pointer gap-1"
              >
                {isSyncing ? (
                  <Loader2 className="size-3 animate-spin" aria-hidden="true" />
                ) : (
                  <RefreshCw className="size-3" aria-hidden="true" />
                )}
                同步
              </Button>
              <Button
                variant="ghost"
                size="xs"
                onClick={() => {
                  setEditingProvider(provider);
                  setDialogOpen(true);
                }}
                disabled={busy !== null}
                aria-label={`编辑 ${provider.name}`}
                className="cursor-pointer"
              >
                <Pencil className="size-3" aria-hidden="true" />
              </Button>
              <Button
                variant="ghost"
                size="xs"
                onClick={() => setPendingDeleteProvider(provider)}
                disabled={busy !== null}
                aria-label={`删除 ${provider.name}`}
                className="cursor-pointer text-destructive hover:bg-destructive/10 hover:text-destructive"
              >
                <Trash2 className="size-3" aria-hidden="true" />
              </Button>
            </div>
          );
        },
        size: 220,
      },
    ],
    [busy, handleSync, handleTest, modelsByProvider],
  );

  const table = useReactTable({
    data: filteredProviders,
    columns,
    state: { expanded },
    onExpandedChange: setExpanded,
    getRowId: (provider) => provider.id,
    getRowCanExpand: () => true,
    getCoreRowModel: getCoreRowModel(),
    getExpandedRowModel: getExpandedRowModel(),
  });

  const columnCount = columns.length;

  return (
    <PageShell
      title="模型提供商"
      description="维护上游接口、密钥与可路由的模型清单。展开一行可以管理该提供商下的模型（启停、设默认、手动添加）。"
      actions={
        <Button
          size="sm"
          onClick={() => {
            setEditingProvider(null);
            setDialogOpen(true);
          }}
          disabled={busy !== null}
          className="cursor-pointer gap-1.5"
        >
          <Plus className="size-3.5" aria-hidden="true" />
          新增提供商
        </Button>
      }
    >
      {providersQuery.isError && (
        <InlineAlert
          action={
            <Button
              variant="outline"
              size="xs"
              onClick={() => void providersQuery.refetch()}
              className="cursor-pointer"
            >
              重试
            </Button>
          }
        >
          {describeFailure(providersQuery.error)}
        </InlineAlert>
      )}

      <Toolbar aria-label="提供商筛选">
        <div className="relative w-full max-w-xs min-w-48">
          <Search
            className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground"
            aria-hidden="true"
          />
          <Input
            value={query}
            onChange={(event) => setQuery(event.target.value)}
            placeholder="按名称 / slug / 地址过滤"
            aria-label="按名称、slug 或接口地址过滤提供商"
            autoComplete="off"
            className="h-8 pl-8 md:text-xs"
          />
          {query && (
            <button
              type="button"
              onClick={() => setQuery('')}
              aria-label="清除过滤"
              className="absolute top-1/2 right-2 -translate-y-1/2 cursor-pointer rounded p-0.5 text-muted-foreground hover:text-foreground"
            >
              <X className="size-3.5" aria-hidden="true" />
            </button>
          )}
        </div>
        <span className="text-[11px] text-muted-foreground">
          {providersQuery.isPending
            ? '正在加载…'
            : query.trim()
              ? `匹配 ${filteredProviders.length} / ${providers.length} 个`
              : `共 ${providers.length} 个提供商`}
        </span>
        {busy !== null && (
          <span role="status" className="ml-auto flex items-center gap-1 text-[11px] text-muted-foreground">
            <Loader2 className="size-3 animate-spin" aria-hidden="true" />
            正在执行操作…
          </span>
        )}
      </Toolbar>

      <TableWrap minWidth="68rem">
        <thead>
          {table.getHeaderGroups().map((headerGroup) => (
            <tr key={headerGroup.id}>
              {headerGroup.headers.map((header) => (
                <th key={header.id} className={TH_CLASS} style={{ width: header.column.getSize() }}>
                  {header.isPlaceholder
                    ? null
                    : flexRender(header.column.columnDef.header, header.getContext())}
                </th>
              ))}
            </tr>
          ))}
        </thead>
        <tbody>
          {providersQuery.isPending ? (
            <TableStatusRow colSpan={columnCount} loading text="正在加载提供商…" />
          ) : filteredProviders.length === 0 ? (
            <TableStatusRow
              colSpan={columnCount}
              text={
                query.trim()
                  ? `没有匹配「${query.trim()}」的提供商`
                  : '还没有配置任何提供商，点右上角「新增提供商」开始。'
              }
              action={
                query.trim() ? (
                  <Button variant="outline" size="xs" onClick={() => setQuery('')} className="cursor-pointer">
                    清除过滤
                  </Button>
                ) : undefined
              }
            />
          ) : (
            table.getRowModel().rows.map((row) => {
              const provider = row.original;
              return (
                // Fragment 必须带 key：兄弟行与展开行是同一对，缺 key 会在切换展开时复用错行
                <Fragment key={row.id}>
                  <tr className={TR_CLASS}>
                    {row.getVisibleCells().map((cell) => (
                      <td key={cell.id} className={TD_CLASS}>
                        {flexRender(cell.column.columnDef.cell, cell.getContext())}
                      </td>
                    ))}
                  </tr>
                  {row.getIsExpanded() && (
                    <tr>
                      <td colSpan={columnCount} className="border-b border-border/60 bg-muted/20 px-3 py-3">
                        <ModelsArea
                          provider={provider}
                          models={modelsByProvider.get(provider.id) ?? []}
                          loading={!modelsByProvider.has(provider.id)}
                          busy={busy}
                          onToggleModel={handleToggleModel}
                          onSetDefault={handleSetDefault}
                          onEditModel={setEditingModel}
                          onShowModelDetail={setDetailModel}
                          onRequestDeleteModel={setPendingDeleteModel}
                          onAddModel={(modelId) => handleAddModel(provider.id, modelId)}
                        />
                      </td>
                    </tr>
                  )}
                </Fragment>
              );
            })
          )}
        </tbody>
      </TableWrap>

      <ProviderDialog
        open={dialogOpen}
        onOpenChange={setDialogOpen}
        provider={editingProvider}
        onSaved={() => void queryClient.invalidateQueries({ queryKey: queryKeys.providers })}
      />

      <ModelDialog
        open={editingModel !== null}
        onOpenChange={(open) => {
          if (!open) setEditingModel(null);
        }}
        model={editingModel}
        providerName={providers.find((p) => p.id === editingModel?.provider_id)?.name ?? ''}
        onSaved={() => {
          if (editingModel) void refreshModels(editingModel.provider_id);
        }}
      />

      <ModelDetailDialog
        open={detailModel !== null}
        onOpenChange={(open) => {
          if (!open) setDetailModel(null);
        }}
        model={detailModel}
        providerName={providers.find((p) => p.id === detailModel?.provider_id)?.name ?? ''}
      />

      <ConfirmDialog
        open={pendingDeleteProvider !== null}
        onOpenChange={(open) => {
          if (!open) setPendingDeleteProvider(null);
        }}
        title="删除提供商"
        description={
          pendingDeleteProvider
            ? `「${pendingDeleteProvider.name}」及其下的全部模型将被删除，且无法恢复。`
            : undefined
        }
        confirmLabel="删除"
        destructive
        onConfirm={() => {
          const target = pendingDeleteProvider;
          setPendingDeleteProvider(null);
          if (!target) return;
          void (async () => {
            try {
              await providerMutation.mutateAsync({ id: target.id });
              notify('success', `已删除提供商 ${target.name}`);
            } catch (err) {
              console.error('删除提供商失败:', err);
              notify('error', describeFailure(err));
            }
          })();
        }}
      />

      <ConfirmDialog
        open={pendingDeleteModel !== null}
        onOpenChange={(open) => {
          if (!open) setPendingDeleteModel(null);
        }}
        title="删除模型"
        description={
          pendingDeleteModel
            ? `模型「${pendingDeleteModel.display_name}」将从列表中移除；下次同步仍可能重新出现。`
            : undefined
        }
        confirmLabel="删除"
        destructive
        onConfirm={() => {
          if (pendingDeleteModel) void handleDeleteModel(pendingDeleteModel);
          setPendingDeleteModel(null);
        }}
      />
    </PageShell>
  );
}

/**
 * 展开行里的模型子表。
 *
 * 自己持有过滤与折叠状态（每次展开都是「我要看这一个提供商的模型」），
 * 数据来自父级的 query 缓存。过滤是纯前端：模型清单本来就整份在手里。
 */
function ModelsArea({
  provider,
  models,
  loading,
  busy,
  onToggleModel,
  onSetDefault,
  onEditModel,
  onShowModelDetail,
  onRequestDeleteModel,
  onAddModel,
}: {
  provider: AdminProvider;
  models: AdminModel[];
  loading: boolean;
  busy: string | null;
  onToggleModel: (model: AdminModel) => void;
  onSetDefault: (model: AdminModel) => void;
  onEditModel: (model: AdminModel) => void;
  onShowModelDetail: (model: AdminModel) => void;
  onRequestDeleteModel: (model: AdminModel) => void;
  onAddModel: (modelId: string) => Promise<boolean>;
}) {
  const [filter, setFilter] = useState('');
  const [showAll, setShowAll] = useState(false);
  const [newModelId, setNewModelId] = useState('');
  const [adding, setAdding] = useState(false);

  const visible: AdminModel[] = [...filterAdminModels(models, filter)];
  // 过滤优先，其次才是展开：一百多个模型全平铺会把页面变成一堵墙
  const collapsed = filter.trim() === '' && !showAll && visible.length > VISIBLE_MODELS_COLLAPSED;
  const shown = collapsed ? visible.slice(0, VISIBLE_MODELS_COLLAPSED) : visible;
  const busyAny = busy !== null || adding;

  const modelColumns = useMemo<ColumnDef<AdminModel>[]>(
    () => [
      {
        id: 'model',
        header: '模型',
        cell: ({ row }) => {
          const model = row.original;
          return (
            <div className="min-w-0">
              <div className="flex flex-wrap items-center gap-1.5">
                <span className="text-xs font-medium" title={model.description ?? model.display_name}>
                  {model.display_name}
                </span>
                {model.reasoning && <Badge tone="primary">推理</Badge>}
                {model.is_default && (
                  <Badge tone="muted">
                    <Star className="size-2.5 fill-current" aria-hidden="true" />
                    默认
                  </Badge>
                )}
                {!model.enabled && <Badge tone="muted">已停用</Badge>}
              </div>
              <code className="mt-0.5 block font-mono text-[11px] break-all text-muted-foreground">
                {model.model_id}
              </code>
            </div>
          );
        },
      },
      {
        id: 'context',
        header: '上下文',
        cell: ({ row }) => (
          <span
            className="text-xs text-muted-foreground"
            title={
              row.original.max_output_tokens
                ? `最大输出 ${row.original.max_output_tokens} tokens`
                : undefined
            }
          >
            {formatContextWindow(row.original.context_window)}
          </span>
        ),
        size: 120,
      },
      {
        id: 'enabled',
        header: '启用',
        cell: ({ row }) => (
          <ToggleSwitch
            checked={row.original.enabled}
            onCheckedChange={() => onToggleModel(row.original)}
            label={`${row.original.enabled ? '停用' : '启用'}模型 ${row.original.display_name}（${row.original.model_id}）`}
            disabled={busyAny}
          />
        ),
        size: 64,
      },
      {
        id: 'actions',
        header: () => <span className="block text-right">操作</span>,
        cell: ({ row }) => {
          const model = row.original;
          return (
            <div className="flex items-center justify-end gap-1">
              <Button
                variant="ghost"
                size="xs"
                onClick={() => onShowModelDetail(model)}
                disabled={busyAny}
                aria-label={`查看 ${model.display_name} 的详情与原始数据`}
                className="cursor-pointer"
              >
                <Info className="size-3" aria-hidden="true" />
              </Button>
              {model.enabled && !model.is_default && (
                <Button
                  variant="ghost"
                  size="xs"
                  onClick={() => onSetDefault(model)}
                  disabled={busyAny}
                  aria-label={`将 ${model.display_name} 设为默认模型`}
                  className="cursor-pointer"
                >
                  设为默认
                </Button>
              )}
              <Button
                variant="ghost"
                size="xs"
                onClick={() => onEditModel(model)}
                disabled={busyAny}
                aria-label={`编辑模型 ${model.display_name}`}
                className="cursor-pointer"
              >
                <Pencil className="size-3" aria-hidden="true" />
              </Button>
              <Button
                variant="ghost"
                size="xs"
                onClick={() => onRequestDeleteModel(model)}
                disabled={busyAny}
                aria-label={`删除模型 ${model.display_name}`}
                className="cursor-pointer text-destructive hover:bg-destructive/10 hover:text-destructive"
              >
                <Trash2 className="size-3" aria-hidden="true" />
              </Button>
            </div>
          );
        },
        size: 190,
      },
    ],
    [busyAny, onEditModel, onRequestDeleteModel, onSetDefault, onShowModelDetail, onToggleModel],
  );

  const subTable = useReactTable({
    data: shown,
    columns: modelColumns,
    getRowId: (model) => model.id,
    getCoreRowModel: getCoreRowModel(),
  });

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-xs font-medium">{provider.name} 的模型</span>
        <span className="text-[11px] text-muted-foreground">
          {models.length === 0 ? '暂无' : `共 ${models.length} 个，已启用 ${models.filter((m) => m.enabled).length} 个`}
        </span>
        <div className="ml-auto flex items-center gap-1.5">
          <div className="relative w-full max-w-56 min-w-40">
            <Search
              className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground"
              aria-hidden="true"
            />
            <Input
              value={filter}
              onChange={(event) => setFilter(event.target.value)}
              placeholder="过滤模型（ID 或名称）"
              aria-label={`过滤 ${provider.name} 的模型`}
              className="h-7 pl-8 md:text-xs"
            />
          </div>
        </div>
      </div>

      <div className="overflow-hidden rounded-lg border border-border/70 bg-background">
        <table className="w-full border-collapse text-left" style={{ minWidth: '40rem' }}>
          <thead>
            {subTable.getHeaderGroups().map((headerGroup) => (
              <tr key={headerGroup.id}>
                {headerGroup.headers.map((header) => (
                  <th key={header.id} className={TH_CLASS} style={{ width: header.column.getSize() }}>
                    {header.isPlaceholder
                      ? null
                      : flexRender(header.column.columnDef.header, header.getContext())}
                  </th>
                ))}
              </tr>
            ))}
          </thead>
          <tbody>
            {loading ? (
              <TableStatusRow colSpan={modelColumns.length} loading text="正在加载模型…" />
            ) : visible.length === 0 ? (
              <TableStatusRow
                colSpan={modelColumns.length}
                text={
                  filter.trim()
                    ? `没有匹配「${filter.trim()}」的模型`
                    : '暂无模型：点「同步模型」从上游拉取，或在下方手动添加。'
                }
                icon={false}
              />
            ) : (
              subTable.getRowModel().rows.map((row) => (
                <tr key={row.id} className={TR_CLASS}>
                  {row.getVisibleCells().map((cell) => (
                    <td key={cell.id} className={TD_CLASS}>
                      {flexRender(cell.column.columnDef.cell, cell.getContext())}
                    </td>
                  ))}
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {collapsed && (
        <Button
          variant="ghost"
          size="xs"
          onClick={() => setShowAll(true)}
          className="w-full cursor-pointer border border-dashed border-border/70 text-muted-foreground"
        >
          展开全部（还有 {visible.length - VISIBLE_MODELS_COLLAPSED} 个）
        </Button>
      )}
      {!collapsed && showAll && filter.trim() === '' && models.length > VISIBLE_MODELS_COLLAPSED && (
        <Button
          variant="ghost"
          size="xs"
          onClick={() => setShowAll(false)}
          className="w-full cursor-pointer border border-dashed border-border/70 text-muted-foreground"
        >
          收起，只看前 {VISIBLE_MODELS_COLLAPSED} 个
        </Button>
      )}

      {/* 用 form 而不是裸按钮：输入框里按回车也能添加 */}
      <form
        onSubmit={(event) => {
          event.preventDefault();
          setAdding(true);
          void onAddModel(newModelId).then((ok) => {
            setAdding(false);
            if (ok) setNewModelId('');
          });
        }}
        className="flex flex-wrap items-center gap-1.5"
      >
        <Input
          value={newModelId}
          onChange={(event) => setNewModelId(event.target.value)}
          placeholder="上游模型 ID，如 deepseek-chat"
          aria-label={`要添加到 ${provider.name} 的上游模型 ID`}
          spellCheck={false}
          autoComplete="off"
          disabled={loading}
          className="h-7 max-w-sm min-w-48 flex-1 md:text-xs"
        />
        <Button
          type="submit"
          variant="outline"
          size="xs"
          disabled={busyAny}
          aria-label={`添加模型到 ${provider.name}`}
          className="cursor-pointer gap-1"
        >
          <Plus className="size-3" aria-hidden="true" />
          添加
        </Button>
      </form>
    </div>
  );
}
